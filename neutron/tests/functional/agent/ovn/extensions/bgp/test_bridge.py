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

from unittest import mock

import netaddr
from oslo_config import cfg
from ovsdbapp.backend.ovs_idl import connection

from neutron.agent.common import ovs_lib
from neutron.agent.linux import ip_lib
from neutron.agent.ovn.extensions.bgp import bridge
from neutron.agent.ovn.extensions.bgp import exceptions as exc
from neutron.common import utils
from neutron.tests.common import net_helpers
from neutron.tests.functional.agent.ovn.extensions import bgp as test_bgp_utils
from neutron.tests.functional.services import bgp
from neutron.tests.functional.services.bgp import fixtures


class BaseBridgeTestCase(bgp.BaseBgpSbIdlTestCase):

    def setUp(self):
        super().setUp()
        self.bridge_name = utils.get_rand_device_name('br-bgp')
        self.ovs_api = self._create_ovs_api()
        self.bgp_agent_api = self._create_bgp_agent_api()

        self.useFixture(net_helpers.OVSBridgeFixture(self.bridge_name))
        self.br = bridge.Bridge(self.bgp_agent_api, self.bridge_name)

    def _create_ovs_api(self):
        ovs_idl = bgp.OvsTestIdl(cfg.CONF.OVS.ovsdb_connection)
        conn = connection.Connection(ovs_idl, timeout=10)
        return self.useFixture(
            fixtures.OvsApiFixture(conn)).obj

    def _create_bgp_agent_api(self):
        class BGPAgentAPI:
            def __init__(self, ovs_api, sb_api):
                self.agent_api = AgentAPI(ovs_api, sb_api)
                self.host_ips = []

        class AgentAPI:
            def __init__(self, ovs_api, sb_api):
                self.ovs_idl = ovs_api
                self.sb_idl = sb_api

        return BGPAgentAPI(self.ovs_api, self.sb_api)


class BridgeTestCase(BaseBridgeTestCase):
    def test__apply_flows_as_bundle(self):
        test_flows = [
            "priority=100,arp actions=NORMAL",
            "priority=100,ip,nw_dst=192.168.1.1 actions=NORMAL",
            "priority=0 actions=drop"
        ]

        pre_apply_flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        # The default NORMAL flow
        self.assertEqual(1, len(pre_apply_flows))

        self.br._apply_flows_as_bundle(test_flows)

        actual_flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        self.assertEqual(len(test_flows), len(actual_flows))

    def test__apply_flows_as_bundle_empty_deletes_flows(self):
        self.br._apply_flows_as_bundle([])

        flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        self.assertFalse(flows)


class BaseBGPChassisBridgeTestCase(BaseBridgeTestCase):
    def setUp(self):
        super().setUp()
        self.br = bridge.BGPChassisBridge(self.bgp_agent_api, self.bridge_name)
        self.peer_br_name = utils.get_rand_device_name('br-bgp-peer')
        self.useFixture(net_helpers.OVSBridgeFixture(self.peer_br_name))
        self.peer_br = ovs_lib.OVSBridge(self.peer_br_name)
        self.fake_nic_port_name = utils.get_rand_device_name('fake-nic')

    def _create_patch_ports(self):
        patch_port_name = utils.get_rand_device_name('patch-to-bgp')
        peer_patch_port_name = utils.get_rand_device_name('patch-to-peer')
        self.br.ovs_bridge.add_patch_port(patch_port_name,
                                           peer_patch_port_name)
        self.peer_br.add_patch_port(peer_patch_port_name, patch_port_name)

        return patch_port_name, peer_patch_port_name


class BGPChassisBridgeTestCase(BaseBGPChassisBridgeTestCase):
    def setUp(self):
        super().setUp()
        self.br.ovs_bridge.add_port(self.fake_nic_port_name)

    def _add_ip(self, ip):
        ip_device = ip_lib.IPDevice(self.bridge_name)
        ip_device.addr.add(str(ip), 'global')

    def test_get_bridge_patch_port_ofport(self):
        self._create_patch_ports()
        # Fake NIC port is in error hence -1 and ofport 1 is the patch port
        expected_ofport = 1
        self.assertEqual(expected_ofport,
                         self.br.get_bridge_patch_port_ofport())

    def test_get_bridge_patch_port_ofport_missing(self):
        self.assertRaises(exc.BridgePatchPortException,
                          self.br.get_bridge_patch_port_ofport)

    def test_get_bridge_patch_port_ofport_multiple_patch_ports(self):
        self._create_patch_ports()
        self._create_patch_ports()
        self.assertRaises(exc.BridgePatchPortException,
                          self.br.get_bridge_patch_port_ofport)

    def test_bridge_ifaces(self):
        patch_port_name, peer_patch_port_name = self._create_patch_ports()
        expected_ifaces = [self.fake_nic_port_name, patch_port_name]
        self.assertEqual(expected_ifaces,
                         [iface['name'] for iface in self.br.bridge_ifaces()])

    def test_get_bridge_nic_ofport(self):
        self._create_patch_ports()
        self._create_patch_ports()
        # The fake NIC port is in error, so it's -1
        expected_ofport = -1
        self.assertEqual(expected_ofport, self.br.get_bridge_nic_ofport())

    def test_get_bridge_nic_ofport_missing(self):
        self.br.ovs_bridge.delete_port(self.fake_nic_port_name)
        self.assertRaises(exc.BridgeNicException,
                          self.br.get_bridge_nic_ofport)

    def test_get_bridge_nic_ofport_multiple_nics(self):
        another_fake_nic_port_name = utils.get_rand_device_name('fake-nic')
        self.br.ovs_bridge.add_port(another_fake_nic_port_name)
        self.assertRaises(exc.BridgeNicException,
                          self.br.get_bridge_nic_ofport)

    def test_ips(self):
        expected_ips = [
            netaddr.IPNetwork('192.168.1.1/30'),
            netaddr.IPNetwork('1.2.3.4/32'),
            netaddr.IPNetwork('5.4.3.2/32'),
        ]
        for ip in expected_ips:
            self._add_ip(str(ip))

        self.assertCountEqual(expected_ips, self.br.ips)

    def test_ips_missing(self):
        self.assertEqual([], self.br.ips)


class BGPChassisBridgeConfigureFlowsTestCase(BaseBGPChassisBridgeTestCase):
    def setUp(self):
        super().setUp()
        self.br.ovs_bridge.add_port(
            self.fake_nic_port_name, ('type', 'internal'))

    def _wait_for_base_flows_are_installed(self):
        utils.wait_until_true(
            lambda: len(test_bgp_utils.dump_flows(self.br.ovs_bridge)) > 5,
            timeout=5,
            exception=Exception(
                "Expected didn't get expected number of flows in 5 seconds")
        )

    def test_configure_flows_no_nic_installs_basic_flows(self):
        self.br.ovs_bridge.delete_port(self.fake_nic_port_name)

        flows_before = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        self.assertGreater(len(flows_before), 0)

        self.br.configure_flows()

        self._wait_for_base_flows_are_installed()

    @mock.patch.object(
            bridge.BGPChassisBridge, 'get_bridge_nic_ofport', return_value=1)
    def test_configure_flows_no_lrp_mac(
            self, m_get_bridge_nic_ofport):
        self._create_patch_ports()

        self.br.configure_flows()
        self._wait_for_base_flows_are_installed()

        flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        flow_strings = ' '.join(flows)

        self.assertIn('arp', flow_strings)
        self.assertIn('icmp6', flow_strings)
        self.assertIn('ipv6', flow_strings)

        self.assertNotIn('mod_dl_dst:', flow_strings)

    @mock.patch.object(
            bridge.BGPChassisBridge, 'get_bridge_nic_ofport', return_value=1)
    def test_configure_flows_no_patch_port_missing_patch_flows(
            self, m_get_bridge_nic_ofport):

        self.br.lrp_mac = "aa:bb:cc:dd:ee:ff"

        self.br.configure_flows()
        self._wait_for_base_flows_are_installed()

        flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        flow_strings = ' '.join(flows) if flows else ''

        self.assertIn('arp', flow_strings)

        self.assertNotIn('mod_dl_dst:', flow_strings)

    @mock.patch.object(
            bridge.BGPChassisBridge, 'get_bridge_nic_ofport', return_value=1)
    def test_configure_flows_complete_has_all_expected_flows(
            self, m_get_bridge_nic_ofport):
        self._create_patch_ports()

        self.br.bgp_agent_api.host_ips = [
            netaddr.IPNetwork('172.16.1.1/30'),
            netaddr.IPNetwork('2001:db8::1/64'),
            netaddr.IPNetwork('192.168.1.10/32'),
            netaddr.IPNetwork('10.0.0.1/32'),
            netaddr.IPNetwork('2001:db8:eee::1/128'),
        ]

        self.br.lrp_mac = "aa:bb:cc:dd:ee:ff"

        self.br.configure_flows()
        self._wait_for_base_flows_are_installed()

        flows = test_bgp_utils.dump_flows(self.br.ovs_bridge)
        flow_strings = ' '.join(flows)

        # 1. ARP and ICMPv6 flows
        self.assertIn('arp actions=NORMAL', flow_strings)
        self.assertIn('icmp6,icmp_type=133 actions=NORMAL',
                        flow_strings)
        self.assertIn('icmp6,icmp_type=134 actions=NORMAL',
                        flow_strings)
        self.assertIn('icmp6,icmp_type=135 actions=NORMAL',
                        flow_strings)
        self.assertIn('icmp6,icmp_type=136 actions=NORMAL',
                        flow_strings)

        # 2. Host IP flows (IPv4)
        self.assertIn('nw_dst=192.168.1.10 actions=NORMAL', flow_strings)
        self.assertIn('nw_dst=10.0.0.1 actions=NORMAL', flow_strings)

        # 3. Host IP flows (IPv6)
        self.assertIn('ipv6_dst=2001:db8:eee::1 actions=NORMAL', flow_strings)
        self.assertIn('ipv6_dst=2001:db8::1 actions=NORMAL', flow_strings)

        # 4. IPv6 link-local traffic
        self.assertIn('ipv6_dst=fe80::/64 actions=NORMAL', flow_strings)

        # 5. Default flow
        self.assertIn('priority=0 actions=NORMAL', flow_strings)

        # 6. LRP MAC rewrite flow
        self.assertIn(
            'in_port=1 actions=mod_dl_dst:aa:bb:cc:dd:ee:ff,output:2',
            flow_strings)
