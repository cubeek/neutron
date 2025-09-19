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

from collections import namedtuple

from oslo_utils import uuidutils
from ovsdbapp.backend.ovs_idl import idlutils

from neutron.services.bgp import commands
from neutron.services.bgp import constants
from neutron.services.bgp import exceptions
from neutron.services.bgp import helpers
from neutron.tests.functional.services import bgp


def _get_unique_name(prefix="test"):
    return f"{prefix}_{uuidutils.generate_uuid()[:8]}"


class NbCommandsBase(bgp.BaseBgpNbIdlTestCase):
    def setUp(self):
        super().setUp()
        mm = helpers.LrpMacManager.get_instance()
        mm.known_routers.clear()


class BgpCommandsBase(bgp.BaseBgpTestCase):
    def setUp(self):
        super().setUp()
        mm = helpers.LrpMacManager.get_instance()
        mm.known_routers.clear()


class _AddBaseCommand:
    table = None
    command = None

    def _assert_table_row_exists(self, name, should_exist=True):
        try:
            row = self.nb_api.lookup(self.table, name)
            if should_exist:
                self.assertIsNotNone(row)
                self.assertEqual(row.name, name)
                return row
            self.fail(f"{self.table} {name} should not exist")
        except idlutils.RowNotFound:
            if should_exist:
                self.fail(f"{self.table} {name} not found")
            return None

    def create_row(self, name, **kwargs):
        self.command(self.nb_api, name, **kwargs).execute(check_error=True)

    def test_create_new_row(self):
        name = _get_unique_name()

        # Verify row doesn't exist initially
        self._assert_table_row_exists(name, should_exist=False)

        self.create_row(name)

        # Verify the row was created
        created_row = self._assert_table_row_exists(name)
        self.assertEqual(created_row.name, name)

    def test_create_existing_row(self):
        name = _get_unique_name()

        # Create row first time
        self.create_row(name)

        # Verify first creation worked
        row1 = self._assert_table_row_exists(name)

        # Create same row again - should be idempotent
        self.create_row(name)

        # Lookup should not fail with duplicated name
        row2 = self._assert_table_row_exists(name)
        self.assertEqual(row1.uuid, row2.uuid)

    def test_external_ids_update_on_create(self):
        name = _get_unique_name()
        self.create_row(name, external_ids={'id1': 'value1'})
        row = self._assert_table_row_exists(name)
        self.assertEqual(row.external_ids.get('id1'), 'value1')

        # Create same row again - should be idempotent
        self.create_row(name, external_ids={'id1': 'value2'})
        row = self._assert_table_row_exists(name)
        self.assertEqual(row.external_ids.get('id1'), 'value2')

        # Create with different name - should create a new row
        name = _get_unique_name()
        self.create_row(name, external_ids={'id1': 'value3'})
        row = self._assert_table_row_exists(name)


class LsAddCommandTestCase(NbCommandsBase, _AddBaseCommand):
    table = 'Logical_Switch'
    command = commands._LsAddCommand


class LrAddCommandTestCase(NbCommandsBase, _AddBaseCommand):
    table = 'Logical_Router'
    command = commands._LrAddCommand


class LspAddCommandTestCase(NbCommandsBase, _AddBaseCommand):
    table = 'Logical_Switch_Port'
    command = commands._LspAddCommand

    def setUp(self):
        super().setUp()
        self.ls_name = _get_unique_name()
        self.nb_api.ls_add(self.ls_name).execute(check_error=True)

    def create_row(self, name, **kwargs):
        return self.command(self.nb_api, self.ls_name, name, **kwargs).execute(
            check_error=True)

    def test_create_existing_with_different_attributes(self):
        name = _get_unique_name()
        self.create_row(
            name, options={'peer-port': 'lsp-peer-1'},
            external_ids={'id1': 'value1'})
        lsp = self._assert_table_row_exists(name)
        self.assertEqual(lsp.options.get('peer-port'), 'lsp-peer-1')
        self.assertEqual(lsp.external_ids.get('id1'), 'value1')

        # Should update the options
        self.create_row(name, options={'peer-port': 'lsp-peer-2'},
                        external_ids={'id1': 'value2'})
        lsp = self._assert_table_row_exists(name)
        self.assertEqual(lsp.options.get('peer-port'), 'lsp-peer-2')
        self.assertEqual(lsp.external_ids.get('id1'), 'value2')


class LrpAddCommandTestCase(NbCommandsBase, _AddBaseCommand):
    table = 'Logical_Router_Port'
    command = commands._LrpAddCommand

    def setUp(self):
        super().setUp()
        self.lr_name = _get_unique_name()
        self.nb_api.lr_add(self.lr_name).execute(check_error=True)

    def create_row(self, name, **kwargs):
        if 'mac' not in kwargs:
            kwargs['mac'] = '00:00:00:00:00:00'
        return self.command(
            self.nb_api, self.lr_name, name, **kwargs).execute(
                check_error=True)

    def test_create_existing_with_different_attributes(self):
        name = _get_unique_name()
        self.create_row(name, mac='00:00:00:00:00:00',
                        networks=['192.168.1.0/24'], peer='lrp-peer-1')
        lrp = self._assert_table_row_exists(name)
        self.assertEqual(lrp.mac, '00:00:00:00:00:00')
        self.assertEqual(lrp.networks, ['192.168.1.0/24'])
        self.assertEqual(lrp.peer, ['lrp-peer-1'])

        # Should update the MAC address
        self.create_row(name, mac='00:00:00:00:00:01',
                        networks=['192.168.2.0/24'], peer='lrp-peer-2')
        lrp = self._assert_table_row_exists(name)
        self.assertEqual(lrp.mac, '00:00:00:00:00:01')
        self.assertEqual(lrp.networks, ['192.168.2.0/24'])
        self.assertEqual(lrp.peer, ['lrp-peer-2'])


class HAChassisGroupAddCommandTestCase(NbCommandsBase, _AddBaseCommand):
    table = 'HA_Chassis_Group'
    command = commands._HAChassisGroupAddCommand


class CreateSwitchWithLocalnetCommandTestCase(NbCommandsBase):
    def _validate_localnet_port(self, ls_name, network_name):
        """Validate localnet port was created correctly"""
        localnet_lsp_name = commands.get_lsp_localnet_name(ls_name)
        lsp = self.nb_api.lookup('Logical_Switch_Port', localnet_lsp_name)
        self.assertEqual(lsp.type, 'localnet')
        self.assertEqual(lsp.options.get('network_name'), network_name)
        self.assertEqual(lsp.addresses, ['unknown'])

    def _create_lsp(self, ls_name, lsp_name, **attrs):
        """Helper to create LSP with wrong attributes"""
        self.nb_api.lsp_add(ls_name, lsp_name, **attrs).execute(
            check_error=True)

    def test_create_new_switch_with_localnet(self):
        ls_name = _get_unique_name()
        network_name = 'test-network'

        commands.CreateSwitchWithLocalnetCommand(
            self.nb_api, ls_name, network_name).execute(check_error=True)

        ls = self.nb_api.ls_get(ls_name).execute(check_error=True)
        self.assertEqual(ls.name, ls_name)

        self._validate_localnet_port(ls_name, network_name)

    def test_create_existing_switch_updates_localnet(self):
        ls_name = _get_unique_name()
        network_name = 'test-network'

        # Create switch first
        self.nb_api.ls_add(ls_name).execute(check_error=True)

        # Execute command
        commands.CreateSwitchWithLocalnetCommand(
            self.nb_api, ls_name, network_name).execute(check_error=True)

        # Verify localnet port was created even with existing switch
        self._validate_localnet_port(ls_name, network_name)

    def test_create_with_existing_localnet_wrong_attributes(self):
        """Test corner case where localnet port exists with wrong attributes"""
        ls_name = _get_unique_name()
        network_name = 'test-network'

        # Create switch and localnet port with wrong attributes
        self.nb_api.ls_add(ls_name).execute(check_error=True)
        localnet_lsp_name = commands.get_lsp_localnet_name(ls_name)
        self._create_lsp(
            ls_name, localnet_lsp_name,
            type='patch',  # wrong type
            options={'wrong': 'value'},  # wrong options
            addresses=['00:00:00:00:00:01']  # wrong addresses
        )

        # Execute command should fix the attributes
        commands.CreateSwitchWithLocalnetCommand(
            self.nb_api, ls_name, network_name).execute(check_error=True)

        # Verify attributes were corrected
        self._validate_localnet_port(ls_name, network_name)


class CreateLspLocalnetCommandTestCase(NbCommandsBase):
    def _validate_localnet_port(self, ls_name, network_name):
        """Validate localnet port was created correctly"""
        localnet_lsp_name = commands.get_lsp_localnet_name(ls_name)
        lsp = self.nb_api.lookup('Logical_Switch_Port', localnet_lsp_name)
        self.assertEqual(lsp.type, 'localnet')
        self.assertEqual(lsp.options.get('network_name'), network_name)
        self.assertEqual(lsp.addresses, ['unknown'])
        return lsp

    def _create_lsp(self, ls_name, lsp_name, **attrs):
        self.nb_api.lsp_add(ls_name, lsp_name, **attrs).execute(
            check_error=True)

    def setUp(self):
        super().setUp()
        self.ls_name = _get_unique_name()
        self.nb_api.ls_add(self.ls_name).execute(check_error=True)

    def test_create_localnet_port(self):
        network_name = 'test-network'

        commands.CreateLspLocalnetCommand(
            self.nb_api, self.ls_name, network_name).execute(check_error=True)

        self._validate_localnet_port(self.ls_name, network_name)

    def test_update_existing_localnet_with_different_network(self):
        network_name1 = 'test-network-1'
        network_name2 = 'test-network-2'

        # Create first localnet port
        commands.CreateLspLocalnetCommand(
            self.nb_api, self.ls_name, network_name1).execute(check_error=True)

        # Update with different network name
        commands.CreateLspLocalnetCommand(
            self.nb_api, self.ls_name, network_name2).execute(check_error=True)

        # Verify network name was updated
        self._validate_localnet_port(self.ls_name, network_name2)

    def test_fix_localnet_with_wrong_type_and_options(self):
        network_name = 'test-network'
        localnet_lsp_name = commands.get_lsp_localnet_name(self.ls_name)

        self._create_lsp(
            self.ls_name, localnet_lsp_name,
            type='router',  # wrong type
            options={'router-port': 'wrong'},  # wrong options
            addresses=['router']  # wrong addresses
        )

        commands.CreateLspLocalnetCommand(
            self.nb_api, self.ls_name, network_name).execute(check_error=True)

        lsp = self._validate_localnet_port(self.ls_name, network_name)
        self.assertNotIn('router-port', lsp.options)


class LrRouteAddCommandTestCase(NbCommandsBase):
    def _validate_route_created(self, prefix, nexthop, port=None):
        routes = self.nb_api.db_find(
            'Logical_Router_Static_Route',
            ('ip_prefix', '=', prefix),
            ('nexthop', '=', nexthop)
        ).execute(check_error=True)
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route['ip_prefix'], prefix)
        self.assertEqual(route['nexthop'], nexthop)
        if port:
            self.assertEqual(route['output_port'], port)

    def setUp(self):
        super().setUp()
        self.lr_name = _get_unique_name()
        self.nb_api.lr_add(self.lr_name).execute(check_error=True)

    def test_add_new_route(self):
        prefix = '192.168.1.0/24'
        nexthop = '10.0.0.1'

        commands._LrRouteAddCommand(
            self.nb_api, self.lr_name, prefix, nexthop).execute(
                check_error=True)

        self._validate_route_created(prefix, nexthop)

    def test_add_route_with_port(self):
        prefix = '192.168.1.0/24'
        nexthop = '10.0.0.1'
        port_name = 'test-port'

        # Execute command with port
        commands._LrRouteAddCommand(
            self.nb_api, self.lr_name, prefix, nexthop,
            port=port_name).execute(check_error=True)

        self._validate_route_created(prefix, nexthop, port_name)

    def test_add_duplicate_route_idempotent(self):
        prefix = '192.168.1.0/24'
        nexthop = '10.0.0.1'

        # Add route first time
        commands._LrRouteAddCommand(
            self.nb_api, self.lr_name, prefix, nexthop,
            may_exist=True).execute(check_error=True)

        # Add same route again
        commands._LrRouteAddCommand(
            self.nb_api, self.lr_name, prefix, nexthop,
            may_exist=True).execute(check_error=True)

        # Should be idempotent
        self._validate_route_created(prefix, nexthop)


class ReconcileRouterCommandTestCase(NbCommandsBase):
    def _validate_router_created(self, router_name):
        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.name, router_name)
        mm = helpers.LrpMacManager.get_instance()
        self.assertIsNotNone(mm.known_routers.get(router_name))

    def test_reconcile_new_router(self):
        router_name = _get_unique_name()

        commands.ReconcileRouterCommand(
            self.nb_api, router_name).execute(check_error=True)

        self._validate_router_created(router_name)

    def test_reconcile_existing_router(self):
        router_name = _get_unique_name()

        self.nb_api.lr_add(router_name).execute(check_error=True)

        commands.ReconcileRouterCommand(
            self.nb_api, router_name).execute(check_error=True)

        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.name, router_name)

    def test_reconcile_router_updates_existing_options(self):
        router_name = _get_unique_name()

        self.nb_api.lr_add(
            router_name, options={'wrong-option': 'value'}).execute(
                check_error=True)

        commands.ReconcileRouterCommand(
            self.nb_api, router_name).execute(check_error=True)

        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.name, router_name)


class ReconcileMainRouterCommandTestCase(NbCommandsBase):
    def _validate_main_router_options(self, router_name):
        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.name, router_name)
        self.assertEqual(router.options.get('dynamic-routing'), 'true')
        self.assertEqual(router.options.get('dynamic-routing-redistribute'),
                         'connected-as-host,nat')

    def test_reconcile_main_router_with_dynamic_routing(self):
        router_name = commands.ReconcileMainRouterCommand.get_name()

        commands.ReconcileMainRouterCommand(
            self.nb_api).execute(check_error=True)

        self._validate_main_router_options(router_name)

    def test_reconcile_updates_existing_main_router_options(self):
        router_name = commands.ReconcileMainRouterCommand.get_name()

        self.nb_api.lr_add(
            router_name,
            options={'dynamic-routing': 'false', 'wrong-option': 'value'}
        ).execute(check_error=True)

        commands.ReconcileMainRouterCommand(
            self.nb_api).execute(check_error=True)

        self._validate_main_router_options(router_name)

    def test_registered_mac_prefix(self):
        cmd = commands.ReconcileMainRouterCommand(
            self.nb_api)
        cmd.execute(check_error=True)
        router_name = cmd.get_name()
        mm = helpers.LrpMacManager.get_instance()
        expected_prefix = cmd.router_mac_prefix
        self.assertEqual(
            expected_prefix,
            mm.known_routers[router_name].mac_prefix)


class ReconcileChassisRouterCommandTestCase(NbCommandsBase):
    def _create_fake_chassis(self):
        class FakeChassis:
            def __init__(self, name):
                self.name = name
                self.hostname = f'chassis-{name}'
                self.external_ids = {constants.OVN_BGP_CHASSIS_INDEX_KEY: '1'}
        return FakeChassis(_get_unique_name())

    def test_reconcile_chassis_router(self):
        router_name = _get_unique_name()
        chassis = self._create_fake_chassis()

        commands.ReconcileChassisRouterCommand(
            self.nb_api, router_name, chassis).execute(check_error=True)

        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.name, router_name)
        self.assertEqual(router.options.get('chassis'), chassis.name)

    def test_reconcile_updates_chassis_router_options(self):
        router_name = _get_unique_name()
        chassis = self._create_fake_chassis()

        self.nb_api.lr_add(
            router_name,
            options={'chassis': 'wrong-chassis', 'other-option': 'value'}
        ).execute(check_error=True)

        commands.ReconcileChassisRouterCommand(
            self.nb_api, router_name, chassis).execute(check_error=True)

        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router.options.get('chassis'), chassis.name)

    def test_registered_mac_prefix(self):
        router_name = _get_unique_name()
        chassis = self._create_fake_chassis()
        cmd = commands.ReconcileChassisRouterCommand(
            self.nb_api, router_name, chassis)
        cmd.execute(check_error=True)
        mm = helpers.LrpMacManager.get_instance()
        self.assertEqual(
            cmd.router_mac_prefix,
            mm.known_routers[router_name].mac_prefix)


class IndexAllChassisTestCase(bgp.BaseBgpSbIdlTestCase):
    def test_index_all_chassis(self):
        self.sb_api.chassis_add(
            _get_unique_name(),
            ['geneve'],
            '192.168.1.100').execute(check_error=True)
        self.sb_api.chassis_add(
            _get_unique_name(),
            ['geneve'],
            '192.168.1.101').execute(check_error=True)
        result = commands.IndexAllChassis(self.sb_api).execute(
            check_error=True)

        expected_indexes = [str(i) for i in range(2)]
        self.assertCountEqual(
            expected_indexes,
            [r.external_ids.get(constants.OVN_BGP_CHASSIS_INDEX_KEY)
             for r in result])

    def test_index_all_chassis_new_chassis_added(self):
        self.test_index_all_chassis()

        self.sb_api.chassis_add(
            _get_unique_name(),
            ['geneve'],
            '192.168.1.102').execute(check_error=True)

        result = commands.IndexAllChassis(self.sb_api).execute(
            check_error=True)

        expected_indexes = [str(i) for i in range(3)]
        self.assertCountEqual(
            expected_indexes,
            [r.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY]
            for r in result])

    def test_index_all_chassis_with_existing_index(self):
        chassis_names = [_get_unique_name() for _ in range(2)]

        for i, chassis_name in enumerate(chassis_names):
            self.sb_api.chassis_add(
                chassis_name,
                ['geneve'],
                f'192.168.1.10{i}').execute(check_error=True)

        commands.IndexAllChassis(self.sb_api).execute(check_error=True)

        # remove chassis with index 0
        self.sb_api.chassis_del(chassis_names[0]).execute(check_error=True)

        for i in range(2):
            self.sb_api.chassis_add(
                _get_unique_name(),
                ['geneve'],
                f'192.168.1.11{i}').execute(check_error=True)

        result = commands.IndexAllChassis(self.sb_api).execute(
            check_error=True)

        expected_indexes = [str(i) for i in range(3)]
        self.assertCountEqual(
            expected_indexes,
            [r.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY]
            for r in result])


class ConnectRouterToMainRouterCommandTestCase(NbCommandsBase):
    class FakeChassis:
        def __init__(self, name, hostname):
            self.name = name
            self.hostname = hostname
            self.external_ids = {constants.OVN_BGP_CHASSIS_INDEX_KEY: '5'}

    def setUp(self):
        super().setUp()

        self.chassis_router_name = _get_unique_name("chassis-router")
        self.main_router_name = commands.ReconcileMainRouterCommand.get_name()

        self.fake_chassis = self._create_fake_chassis()
        self.chassis_index = int(
            self.fake_chassis.external_ids[
                constants.OVN_BGP_CHASSIS_INDEX_KEY])

        hcg_name = f'bgp-hcg-{self.fake_chassis.hostname}'
        self.hcg_id = self._create_hcg(hcg_name)

        commands.ReconcileMainRouterCommand(
            self.nb_api).execute(check_error=True)
        commands.ReconcileChassisRouterCommand(
            self.nb_api,
            self.chassis_router_name,
            self.fake_chassis).execute(check_error=True)

    def _create_fake_chassis(self):
        chassis_name = _get_unique_name("chassis")
        hostname = f'host-{chassis_name}'
        return self.FakeChassis(chassis_name, hostname)

    def _create_hcg(self, hcg_name):
        return commands._HAChassisGroupAddCommand(
            self.nb_api, hcg_name).execute(check_error=True).uuid

    def _validate_connection_created(self):
        lrp_main_name = commands.get_lrp_name(
            self.main_router_name, self.chassis_router_name)
        lrp_chassis_name = commands.get_lrp_name(
            self.chassis_router_name, self.main_router_name)

        lrp_main = self.nb_api.lrp_get(lrp_main_name).execute(check_error=True)
        lrp_chassis = self.nb_api.lrp_get(
            lrp_chassis_name).execute(check_error=True)

        # Check ports are connected
        self.assertEqual(lrp_main.peer, [lrp_chassis_name])
        self.assertEqual(lrp_chassis.peer, [lrp_main_name])

        # Check MAC addresses
        mm = helpers.LrpMacManager.get_instance()
        expected_main_mac = mm.get_mac_address(
            self.main_router_name, self.chassis_index)
        expected_chassis_mac = mm.get_mac_address(
            self.chassis_router_name, constants.LRP_CHASSIS_TO_MAIN_ROUTER)

        self.assertEqual(expected_main_mac, lrp_main.mac)
        self.assertEqual(expected_chassis_mac, lrp_chassis.mac)

        # Verify IP addresses
        expected_chassis_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_CHASSIS_TO_MAIN_ROUTER)
        expected_main_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)

        self.assertEqual([expected_chassis_ip], lrp_chassis.networks)
        self.assertEqual([expected_main_ip], lrp_main.networks)

        # Verify main router LRP has HA chassis group
        self.assertEqual(self.hcg_id, lrp_main.ha_chassis_group[0].uuid)

        # Verify main router LRP has dynamic routing option
        self.assertEqual(
            'true', lrp_main.options.get('dynamic-routing-maintain-vrf'))

        return lrp_main_name, lrp_chassis_name

    def test_connect_router_to_main_router_new(self):
        commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        ).execute(check_error=True)

        self._validate_connection_created()

    def test_connect_existing_lrps_get_updated(self):
        lrp_main_name = commands.get_lrp_name(
            self.main_router_name, self.chassis_router_name)
        lrp_chassis_name = commands.get_lrp_name(
            self.chassis_router_name, self.main_router_name)

        self.nb_api.lrp_add(
            self.chassis_router_name, lrp_chassis_name,
            mac='00:00:00:00:00:01',  # wrong MAC
            networks=['10.0.0.1/24'],  # wrong IP
            peer='wrong-peer'  # wrong peer
        ).execute(check_error=True)

        self.nb_api.lrp_add(
            self.main_router_name, lrp_main_name,
            mac='00:00:00:00:00:02',  # wrong MAC
            networks=['10.0.0.2/24'],  # wrong IP
            peer='wrong-peer'  # wrong peer
        ).execute(check_error=True)

        commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        ).execute(check_error=True)

        self._validate_connection_created()

    def test_connect_router_is_idempotent(self):
        commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        ).execute(check_error=True)

        lrp_main1, lrp_chassis1 = self._validate_connection_created()

        commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        ).execute(check_error=True)

        lrp_main2, lrp_chassis2 = self._validate_connection_created()

        self.assertEqual(lrp_main1, lrp_main2)
        self.assertEqual(lrp_chassis1, lrp_chassis2)

    def test_connect_router_to_non_existing_main_router(self):
        self.nb_api.lr_del(self.main_router_name).execute(check_error=True)
        cmd = commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        )
        self.assertRaises(
            exceptions.ReconcileError,
            cmd.execute,
            check_error=True
        )

    def test_connect_non_existing_router_to_main_router(self):
        self.nb_api.lr_del(self.chassis_router_name).execute(check_error=True)
        cmd = commands.ConnectRouterToMainRouterCommand(
            self.nb_api, self.chassis_router_name, self.fake_chassis,
            self.hcg_id
        )
        self.assertRaises(
            exceptions.ReconcileError,
            cmd.execute,
            check_error=True
        )


class ConnectRouterToSwitchCommandTestCase(NbCommandsBase):
    def setUp(self):
        super().setUp()
        self.lr_name = _get_unique_name()
        self.ls_name = _get_unique_name()
        self.lrp_mac = '00:01:02:03:04:05'

        self.nb_api.lr_add(self.lr_name).execute(check_error=True)
        self.nb_api.ls_add(self.ls_name).execute(check_error=True)

    @bgp.requires_ovn_version_with_bgp()
    def test_connect_router_to_switch_without_ip(self):
        commands.ConnectRouterToSwitchCommand(
            self.nb_api, self.lr_name, self.ls_name, self.lrp_mac
        ).execute(check_error=True)

        lrp_name = commands.get_lrp_name(self.lr_name, self.ls_name)
        lrp = self.nb_api.lrp_get(lrp_name).execute(check_error=True)
        self.assertEqual(self.lrp_mac, lrp.mac)
        self.assertEqual([], lrp.networks)

        lsp_name = commands.get_lsp_name(self.ls_name, self.lr_name)
        lsp = self.nb_api.lsp_get(lsp_name).execute(check_error=True)
        self.assertEqual('router', lsp.type)
        self.assertEqual(['router'], lsp.addresses)
        self.assertEqual(lrp_name, lsp.options.get('router-port'))

    def test_connect_router_to_switch_with_ip(self):
        lrp_ip = '192.168.1.1/24'

        commands.ConnectRouterToSwitchCommand(
            self.nb_api, self.lr_name, self.ls_name, self.lrp_mac, lrp_ip
        ).execute(check_error=True)

        lrp_name = commands.get_lrp_name(self.lr_name, self.ls_name)
        lrp = self.nb_api.lrp_get(lrp_name).execute(check_error=True)
        self.assertEqual(self.lrp_mac, lrp.mac)
        self.assertEqual([lrp_ip], lrp.networks)

    @bgp.requires_ovn_version_with_bgp()
    def test_connect_existing_with_different_attributes(self):
        lrp_name = commands.get_lrp_name(self.lr_name, self.ls_name)
        lsp_name = commands.get_lsp_name(self.ls_name, self.lr_name)

        # Create LRP and LSP with wrong attributes
        self.nb_api.lrp_add(
            self.lr_name, lrp_name,
            mac='00:00:00:00:00:01',  # wrong MAC
            networks=['10.0.0.1/24']  # wrong networks
        ).execute(check_error=True)

        self.nb_api.lsp_add(
            self.ls_name, lsp_name,
            type='patch',  # wrong type
            addresses=['00:00:00:00:00:01'],  # wrong addresses
            options={'peer': 'wrong-peer'}  # wrong options
        ).execute(check_error=True)

        # Execute command should fix attributes
        commands.ConnectRouterToSwitchCommand(
            self.nb_api, self.lr_name, self.ls_name, self.lrp_mac
        ).execute(check_error=True)

        # Verify attributes were corrected
        lrp = self.nb_api.lrp_get(lrp_name).execute(check_error=True)
        self.assertEqual(self.lrp_mac, lrp.mac)
        self.assertEqual([], lrp.networks)

        lsp = self.nb_api.lsp_get(lsp_name).execute(check_error=True)
        self.assertEqual('router', lsp.type)
        self.assertEqual(['router'], lsp.addresses)
        self.assertEqual(lrp_name, lsp.options.get('router-port'))


class ConnectChassisRouterToSwitchCommandTestCase(NbCommandsBase):
    def setUp(self):
        super().setUp()
        self.lr_name = _get_unique_name()
        self.ls_name = _get_unique_name()
        self.lrp_mac = '00:01:02:03:04:05'

        self.nb_api.lr_add(self.lr_name).execute(check_error=True)
        self.nb_api.ls_add(self.ls_name).execute(check_error=True)

    def test_sets_external_ids(self):
        network_name = _get_unique_name()
        lrp_ip = '192.168.1.1/24'
        commands.ConnectChassisRouterToSwitchCommand(
            self.nb_api, self.lr_name, self.ls_name, self.lrp_mac,
            lrp_ip, network_name
        ).execute(check_error=True)

        lrp_name = commands.get_lrp_name(self.lr_name, self.ls_name)
        lrp = self.nb_api.lrp_get(lrp_name).execute(check_error=True)
        self.assertEqual(
            network_name,
            lrp.external_ids[constants.BGP_CHASSIS_NETWORK_NAME])


class ReconcileChassisCommandTestCase(BgpCommandsBase):
    PeerConnectionAttributes = namedtuple('PeerConnectionAttributes',
                                          ['lrp_name', 'lrp_ip', 'switch_ip'])

    def _create_chassis(
            self, name=None, hostname=None, index=None, peer_connections=None):
        chassis_name = name or _get_unique_name("chassis")
        chassis_hostname = hostname or f'host-{chassis_name}.example.com'

        chassis_external_ids = {}
        if peer_connections:
            chassis_external_ids[
                constants.CHASSIS_PEER_CONNECTIONS] = peer_connections
        if index is not None:
            chassis_external_ids[
                constants.OVN_BGP_CHASSIS_INDEX_KEY] = str(index)

        self.sb_api.chassis_add(
            chassis_name, ['geneve'], f'172.24.4.{index or 0}',
            external_ids=chassis_external_ids,
            hostname=chassis_hostname
        ).execute(check_error=True)

        return self.sb_api.db_list_rows('Chassis', [chassis_name]).execute(
            check_error=True)[0]

    def _validate_hcg_created(self, hostname):
        hcg_name = commands.get_hcg_name(hostname)
        hcg = self.nb_api.db_find(
            'HA_Chassis_Group',
            ('name', '=', hcg_name)
        ).execute(check_error=True)
        self.assertTrue(hcg)
        return hcg[0]

    def _validate_chassis_router_created(self, hostname, chassis_name):
        router_name = commands.get_chassis_router_name(hostname)
        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(router_name, router.name)
        self.assertEqual(chassis_name, router.options.get('chassis'))
        return router

    def _validate_main_router_connection(self, hostname):
        chassis_router_name = commands.get_chassis_router_name(hostname)
        main_router_name = commands.ReconcileMainRouterCommand.get_name()

        lrp_main_name = commands.get_lrp_name(
            main_router_name, chassis_router_name)
        lrp_chassis_name = commands.get_lrp_name(
            chassis_router_name, main_router_name)

        lrp_main = self.nb_api.lrp_get(lrp_main_name).execute(check_error=True)
        lrp_chassis = self.nb_api.lrp_get(lrp_chassis_name).execute(
            check_error=True)

        self.assertEqual([lrp_chassis_name], lrp_main.peer)
        self.assertEqual([lrp_main_name], lrp_chassis.peer)

    def _validate_chassis_router_routes(self, router, peers):
        self.assertEqual(len(peers), len(router.static_routes))
        self.assertEqual(len(peers), len(router.policies))

        # Expected routes
        prefix_nexthop_port_expected = [
            ('0.0.0.0/0', peer.switch_ip, [peer.lrp_name])
            for peer in peers
        ]
        # Actual routes
        prefix_nexthop_port = [
            (route.ip_prefix, route.nexthop, route.output_port)
            for route in router.static_routes
        ]
        self.assertCountEqual(
            prefix_nexthop_port_expected, prefix_nexthop_port)

        main_router_lrp_ip = helpers.InternalIpManager.get_ip(
            chassis_index=1, port_index=constants.LRP_MAIN_ROUTER_TO_CHASSIS)
        # Expected policies
        expected_policies = [
            (f'inport==\"{peer.lrp_name}\"', 'reroute', [main_router_lrp_ip])
            for peer in peers
        ]
        # Actual policies
        actual_policies = [
            (policy.match, policy.action, policy.nexthops)
            for policy in router.policies

        ]
        self.assertCountEqual(expected_policies, actual_policies)

    def _validate_chassis_peer_resources(
            self, chassis, router, switch_ips, lrp_ips):
        mm = helpers.LrpMacManager.get_instance()
        hostname = chassis.hostname.split('.')[0]
        chassis_router_name = commands.get_chassis_router_name(hostname)
        lrp_mac_map = {}
        peers = []

        for peer_index, network_name in enumerate(['physnet1', 'physnet2']):
            switch_name = f'bgp-ls-{hostname}-{network_name}'
            switch = self.nb_api.ls_get(switch_name).execute(check_error=True)
            self.assertEqual(switch_name, switch.name)

            localnet_lsp_name = commands.get_lsp_localnet_name(switch_name)
            lsp = self.nb_api.lsp_get(localnet_lsp_name).execute(
                check_error=True)
            self.assertEqual('localnet', lsp.type)
            self.assertEqual(network_name, lsp.options.get('network_name'))

            lsp_to_router = self.nb_api.lsp_get(
                commands.get_lsp_name(switch_name, chassis_router_name)
            ).execute(check_error=True)
            self.assertEqual('router', lsp_to_router.type)
            self.assertEqual(['router'], lsp_to_router.addresses)

            chassis_router_lrp_name = lsp_to_router.options.get('router-port')
            chassis_router_lrp = self.nb_api.lrp_get(
                chassis_router_lrp_name).execute(check_error=True)
            expected_mac = mm.get_mac_address(
                chassis_router_name,
                constants.LRP_CHASSIS_ROUTER_TO_CHASSIS_SWITCH + peer_index
            )
            self.assertEqual(
                expected_mac,
                chassis_router_lrp.mac,
            )
            lrp_mac_map[network_name] = expected_mac
            self.assertEqual(
                [lrp_ips[peer_index]], chassis_router_lrp.networks)
            peers.append(
                self.PeerConnectionAttributes(
                    chassis_router_lrp_name,
                    lrp_ips[peer_index],
                    switch_ips[peer_index]
            ))

        self._validate_chassis_router_routes(router, peers)

    def test_reconcile_chassis_basic(self):
        chassis = self._create_chassis(index=1)

        # Create main router first (prerequisite)
        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis
        ).execute(check_error=True)


        # Validate all components were created
        chassis_hostname = chassis.hostname.split('.')[0]
        self._validate_hcg_created(chassis_hostname)
        self._validate_chassis_router_created(chassis_hostname, chassis.name)
        self._validate_main_router_connection(chassis_hostname)

    def test_reconcile_chassis_with_peer_connections(self):
        peer_connections = ("physnet1;192.168.1.1/30;192.168.1.2,"
                            "physnet2;10.0.0.1/30;10.0.0.2")
        switch_ips = ["192.168.1.2", "10.0.0.2"]
        lrp_ips = ["192.168.1.1/30", "10.0.0.1/30"]

        chassis = self._create_chassis(
            index=1, peer_connections=peer_connections)
        hostname = chassis.hostname.split('.')[0]

        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis
        ).execute(check_error=True)

        self._validate_hcg_created(hostname)
        router = self._validate_chassis_router_created(hostname, chassis.name)
        self._validate_main_router_connection(hostname)
        self._validate_chassis_peer_resources(
            chassis, router, switch_ips, lrp_ips)

    def test_reconcile_chassis_without_peer_connections(self):
        chassis = self._create_chassis(index=1)
        hostname = chassis.hostname.split('.')[0]

        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis
        ).execute(check_error=True)

        self._validate_hcg_created(hostname)
        self._validate_chassis_router_created(hostname, chassis.name)
        self._validate_main_router_connection(hostname)

    def test_reconcile_chassis_idempotent(self):
        peer_connections = ("physnet1;192.168.1.1/30;192.168.1.2,"
                            "physnet2;10.0.0.1/30;10.0.0.2")
        switch_ips = ["192.168.1.2", "10.0.0.2"]
        lrp_ips = ["192.168.1.1/30", "10.0.0.1/30"]

        chassis = self._create_chassis(
            index=1, peer_connections=peer_connections)
        hostname = chassis.hostname.split('.')[0]

        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        cmd = commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis)
        cmd.execute(check_error=True)
        # Run again to check idempotency
        cmd.execute(check_error=True)

        self._validate_hcg_created(hostname)
        router = self._validate_chassis_router_created(hostname, chassis.name)
        self._validate_main_router_connection(hostname)
        self._validate_chassis_peer_resources(
            chassis, router, switch_ips, lrp_ips)

    def test_reconcile_chassis_with_existing_components(self):
        chassis = self._create_chassis(index=1)
        hostname = chassis.hostname.split('.')[0]
        hcg_name = commands.get_hcg_name(hostname)
        router_name = commands.get_chassis_router_name(hostname)

        # Create main router first
        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        # Pre-create HCG with different settings
        self.nb_api.ha_chassis_group_add(hcg_name).execute(check_error=True)

        # Pre-create router with wrong chassis
        self.nb_api.lr_add(
            router_name,
            options={'chassis': 'wrong-chassis'}
        ).execute(check_error=True)

        commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis
        ).execute(check_error=True)

        # Validate components were updated correctly
        router = self.nb_api.lr_get(router_name).execute(check_error=True)
        self.assertEqual(chassis.name, router.options.get('chassis'))

    def test_reconcile_chassis_missing_main_router(self):
        chassis = self._create_chassis(index=1)

        cmd = commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis)
        self.assertRaises(
            exceptions.ReconcileError,
            cmd.execute,
            check_error=True
        )

    def test_reconcile_chassis_invalid_index(self):
        chassis = self._create_chassis(index=1)
        chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY] = 'invalid'

        cmd = commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis)
        self.assertRaises(
            exceptions.ReconcileError,
            cmd.execute,
            check_error=True
        )

    def test_reconcile_chassis_missing_index(self):
        chassis = self._create_chassis()

        self.assertRaises(
            exceptions.ReconcileError,
            commands.ReconcileChassisCommand,
            self.nb_api, self.sb_api, chassis,
        )

    def test_reconcile_chassis_mac_manager_registration(self):
        chassis = self._create_chassis(index=1)
        hostname = chassis.hostname.split('.')[0]

        # Create main router first
        commands.ReconcileMainRouterCommand(self.nb_api).execute(
            check_error=True)

        commands.ReconcileChassisCommand(
            self.nb_api, self.sb_api, chassis
        ).execute(check_error=True)

        # Verify router is registered with MAC manager
        mm = helpers.LrpMacManager.get_instance()
        router_name = commands.get_chassis_router_name(hostname)
        self.assertIn(router_name, mm.known_routers)

        # Verify MAC prefix is correct based on chassis index
        generated_mac = mm.get_mac_address(router_name, 1)

        # MAC prefix should be based on chassis index
        expected_mac = '00:96:00:01:00:01'
        self.assertEqual(expected_mac, generated_mac)
