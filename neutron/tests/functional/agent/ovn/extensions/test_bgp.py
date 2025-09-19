# Copyright 2025 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import re
import tempfile
from unittest import mock
import weakref

from oslo_config import cfg
from oslo_log import log as logging
from ovsdbapp import venv

from neutron.agent.common import ovs_lib
from neutron.agent.ovn.extensions.bgp import events
from neutron.agent.ovsdb import impl_idl
from neutron.common import utils
from neutron.services.bgp import constants
from neutron.tests.common import net_helpers
from neutron.tests.functional.agent.ovn.agent import test_ovn_neutron_agent
from neutron.tests.functional.agent.ovn.extensions import bgp as test_bgp_utils

RE_OVSDB_CONNECTION_DIR = re.compile(
    r"unix:(?P<dir_path>[^/]*(?:/[^/]+)*)/[^/]+$")
LOG = logging.getLogger(__name__)


class TestBridge(ovs_lib.OVSBridge):
    def __init__(self, name):
        super().__init__(name)
        local_connection = cfg.CONF.OVS.ovsdb_connection
        match = RE_OVSDB_CONNECTION_DIR.search(local_connection)
        if match:
            dir_path = match.group('dir_path')
            self._patched_ofctl_name = os.path.join(dir_path, f'{name}.mgmt')
            LOG.debug("Patched bridge name %s to %s",
                      name, self._patched_ofctl_name)
        else:
            self._patched_ofctl_name = name
        self._orig_br_name = name

    def run_ofctl(self, *args, **kwargs):
        self.br_name = self._patched_ofctl_name
        try:
            return super().run_ofctl(*args, **kwargs)
        finally:
            self.br_name = self._orig_br_name


class BGPExtensionTestCase(test_ovn_neutron_agent.TestOVNNeutronAgentBase):
    def setUp(self, **kwargs):
        self.ovs_venv = self.useFixture(venv.OvsVenvFixture(
            tempfile.mkdtemp(),
            remove=True,
        ))
        _orig_ovsdb_connection = cfg.CONF.OVS.ovsdb_connection
        cfg.CONF.set_override(
            'ovsdb_connection', self.ovs_venv.ovs_connection, group='OVS')
        self.addCleanup(
            cfg.CONF.set_override,
            'ovsdb_connection',
            _orig_ovsdb_connection,
            group='OVS')
        # Cleanup after tests that did not cleanup after themselves
        self._reset_class_attributes()
        test_ovn_neutron_agent.EXTENSION_NAMES[
            constants.AGENT_BGP_EXT_NAME] = 'BGP agent extension'
        try:
            super().setUp(extensions=[constants.AGENT_BGP_EXT_NAME], **kwargs)
        finally:
            self.addCleanup(self._reset_class_attributes)

    @property
    def bgp_agent(self):
        return self.ovn_agent[constants.AGENT_BGP_EXT_NAME]

    def _reset_class_attributes(self):
        # The connection is a class attribute so we need to reset it in order
        # to connect to the newly spawned per-test ovsdb server
        impl_idl.NeutronOvsdbIdl._klass._ovsdb_connection = None

        # We spawn a different ovsdb server for each test
        # let's make sure the connection is always a new one
        impl_idl._connection = None

        # We also need to reset the SingletonDecorator
        utils.SingletonDecorator._singleton_instances = (
            weakref.WeakValueDictionary())

        # EXTENSION_NAMES is a class attribute so we need to reset it for other
        # tests running in the same process
        try:
            del test_ovn_neutron_agent.EXTENSION_NAMES[
                constants.AGENT_BGP_EXT_NAME]
        except KeyError:
            pass

    def _create_chassis(self, chassis_name):
        self.sb_api.chassis_add(
            chassis_name,
            ['geneve'],
            '192.168.1.100',
            hostname=chassis_name).execute(
                check_error=True)

    def _add_bgp_bridge(self, bridge_name):
        self.ovn_agent.ovs_idl.add_br(bridge_name).execute(
            check_error=True)
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'bgp-peer-bridges': bridge_name}).execute(
                check_error=True)

        # Wait until the agent picks up the bridge name
        utils.wait_until_true(
            lambda: bridge_name in self.bgp_agent.bgp_bridges,
            timeout=10, exception=Exception(
                'Bridge %s not added or not detected by '
                'BGPChassisBridge' % bridge_name))

        # We need to replace the OVSBridge type to the one that talks to local
        # ovsdb server dedicated to this test
        bgp_bridge = self.bgp_agent.bgp_bridges[bridge_name]
        bgp_bridge.ovs_bridge = TestBridge(bridge_name)

        # Add a fake NIC port to the bridge
        bgp_bridge.ovs_bridge.add_port('fake-nic', ('type', 'internal'))
        ofport = bgp_bridge.ovs_bridge.get_port_ofport('fake-nic')

        # This is to simulate patch port to br-int
        fake_br_it = self.useFixture(
            net_helpers.OVSBridgeFixture('fake-br-int')).bridge
        fake_br_it.add_patch_port('patch-to-int', 'patch-to-bgp')
        bgp_bridge.ovs_bridge.add_patch_port('patch-to-bgp', 'patch-to-int')

        mock.patch.object(
            bgp_bridge.__class__,
            'get_bridge_nic_ofport',
            return_value=ofport).start()

        return bgp_bridge.ovs_bridge

    def _get_bridge_mappings(self):
        bms = self.ovn_agent.ovs_idl.db_get(
            'Open_vSwitch', '.',
            'external_ids').execute(
                check_error=True).get('ovn-bridge-mappings')
        if bms:
            return sorted(bms.split(','))
        return []

    def _check_bridge_mappings(self, expected_bms):
        if expected_bms:
            expected_bms = sorted(expected_bms.split(','))
        def wait_for_bms():
            bms = self._get_bridge_mappings()
            return bms == expected_bms

        utils.wait_until_true(
            wait_for_bms,
            sleep=0.1,
            timeout=5,
            exception=Exception(
                "Expected bridge mappings %s were not configured, got %s" %
                (expected_bms, self._get_bridge_mappings())
            )
        )

    def _remove_update_event(self):
        self.ovn_agent.ovs_idl.idl.notify_handler.unwatch_event(
            events.UpdateLocalOVSEvent(self.ovn_agent))

    def _test_load_bgp_bridges(self, expected_bridge_names, bridge_names):
        if bridge_names:
            self.ovn_agent.ovs_idl.db_set(
                'Open_vSwitch', '.',
                external_ids={'bgp-peer-bridges': bridge_names}).execute(
                    check_error=True)
        self.bgp_agent.load_bgp_bridges()
        observed_bridge_names = [
            b.name for b in self.bgp_agent.bgp_bridges.values()]
        self.assertCountEqual(expected_bridge_names, observed_bridge_names)

    def test_load_bgp_bridges(self):
        self._test_load_bgp_bridges(
            expected_bridge_names=['bgp-br-1', 'bgp-br-2'],
            bridge_names='bgp-br-1,bgp-br-2')

    def test_load_bgp_bridges_empty(self):
        self._test_load_bgp_bridges(
            expected_bridge_names=[],
            bridge_names='')

    def test_load_bgp_bridges_missing_one_bridge(self):
        self._test_load_bgp_bridges(
            expected_bridge_names=['bgp-br-1'],
            bridge_names='bgp-br-1,')

    def test_bgp_extension_configures_bridge_mappings(self):
        # We need to remove the update event to avoid the bridge mappings
        # being updated by the update event, then we restart to trigger the
        # CreateLocalOVSEvent
        self._remove_update_event()
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'ovn-bridge-mappings': 'physnet:bridge'}).execute(
                check_error=True)
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'bgp-peer-bridges': 'bgp-br-1,bgp-br-2'}).execute(
                check_error=True)
        self.ovn_agent.ovs_idl.restart_connection()

        expected_bms = 'physnet:bridge,bgp-br-1:bgp-br-1,bgp-br-2:bgp-br-2'
        self._check_bridge_mappings(expected_bms)

    def test_bgp_extension_configures_bridge_mappings_with_empty_bms(self):
        # We need to remove the update event to avoid the bridge mappings
        # being updated by the update event, then we restart to trigger the
        # CreateLocalOVSEvent
        self._remove_update_event()
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'bgp-peer-bridges': 'bgp-br-1,bgp-br-2'}).execute(
                check_error=True)
        self.ovn_agent.ovs_idl.restart_connection()

        expected_bms = 'bgp-br-1:bgp-br-1,bgp-br-2:bgp-br-2'
        self._check_bridge_mappings(expected_bms)

    def test_bgp_extension_missing_bgp_peer_bridges(self):
        # We need to remove the update event to avoid the bridge mappings
        # being updated by the update event, then we restart to trigger the
        # CreateLocalOVSEvent
        self._remove_update_event()
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'ovn-bridge-mappings': 'physnet:bridge'}).execute(
                check_error=True)
        self.ovn_agent.ovs_idl.restart_connection()

        expected_bms = 'physnet:bridge'
        self._check_bridge_mappings(expected_bms)

    def test_bridge_add_remove_bgp_peer_bridges(self):
        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'bgp-peer-bridges': 'bgp-br-1,bgp-br-2'}).execute(
                check_error=True)

        expected_bms = 'bgp-br-1:bgp-br-1,bgp-br-2:bgp-br-2'
        self._check_bridge_mappings(expected_bms)

        self.ovn_agent.ovs_idl.db_set(
            'Open_vSwitch', '.',
            external_ids={'bgp-peer-bridges': 'bgp-br-1'}).execute(
                check_error=True)

        expected_bms = 'bgp-br-1:bgp-br-1'
        self._check_bridge_mappings(expected_bms)

    def test_adding_chassis_implements_flows(self):
        # First delete the chassis
        self.sb_api.chassis_del(self.chassis_name).execute(check_error=True)

        bridge_name = utils.get_rand_device_name('br-bgp')
        br = self._add_bgp_bridge(bridge_name)

        self._create_chassis(self.chassis_name)

        # wait until the agent implements the flows
        utils.wait_until_true(
            lambda: len(test_bgp_utils.dump_flows(br)) > 2,
            timeout=10, sleep=0.1, exception=Exception(
                'Flows not implemented for %s' % bridge_name))

    def test_adding_foreign_chassis_does_nothing(self):
        self.sb_api.chassis_del(self.chassis_name).execute(check_error=True)

        bridge_name = utils.get_rand_device_name('br-bgp')
        br = self._add_bgp_bridge(bridge_name)

        self._create_chassis('foreign-chassis')

        # wait until OVS implements the NORMAL action flow
        utils.wait_until_true(
            lambda: len(test_bgp_utils.dump_flows(br)) == 1,
            timeout=10, sleep=0.1, exception=Exception(
                'Flows not implemented for %s' % bridge_name))

        # wait until OVS implements the NORMAL action flow
        utils.wait_until_true(
            lambda: len(test_bgp_utils.dump_flows(br)) == 1,
            timeout=10, sleep=0.1, exception=Exception(
                'Flows not implemented for %s' % bridge_name))
