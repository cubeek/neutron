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

import uuid

from oslo_log import log
from ovsdbapp.backend.ovs_idl import command as cmd
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp.backend.ovs_idl import rowview
from ovsdbapp.schema.ovn_northbound import commands as nb_cmd

from neutron.plugins.ml2.drivers.ovn.mech_driver.ovsdb import commands as n_nb_cmd
from neutron.services.bgp import constants
from neutron.services.bgp import config
from neutron.services.bgp import helpers

LOG = log.getLogger(__name__)


def _run_idl_command(cmd, txn):
    cmd.run_idl(txn)
    return cmd.result


def connect_main_router_to_provider_switch(
        api, router_name, provider_switch_name, txn):
    mm = helpers.LrpMacManager.get_instance()
    lrp_mac = mm.get_mac_address(
        router_name, constants.LRP_MAIN_ROUTER_TO_NEUTRON_SWITCH)
    ConnectRouterToSwitchCommand(
        api,
        router_name,
        provider_switch_name,
        lrp_mac=lrp_mac,
    ).run_idl(txn)


def get_lrp_name(from_name, to_name):
    return f'bgp-lrp-{from_name}-to-{to_name}'


def get_lsp_name(from_name, to_name):
    return f'bgp-lsp-{from_name}-to-{to_name}'


def get_lsp_localnet_name(switch_name):
    return f'bgp-lsp-{switch_name}-localnet'


class LsAddCommand(nb_cmd.LsAddCommand):
    def run_idl(self, txn):
        self.may_exist = True
        super().run_idl(txn)

        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup('Logical_Switch', self.result)


class LrAddCommand(nb_cmd.LrAddCommand):
    def run_idl(self, txn):
        self.may_exist = True
        super().run_idl(txn)

        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup('Logical_Router', self.result)


class LspAddCommand(nb_cmd.LspAddCommand):
    def run_idl(self, txn):
        self.may_exist = True
        super().run_idl(txn)

        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup('Logical_Switch_Port', self.result)


class LrpAddCommand(nb_cmd.LrpAddCommand):
    def __init__(self, api, router_name, lrp_name, mac, networks=None, **kwargs):
        networks = networks or []
        super().__init__(api, router_name, lrp_name, mac, networks, **kwargs)

    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Router_Port', self.port).uuid
        except idlutils.RowNotFound:
            super().run_idl(txn)


class HAChassisGroupAddCommand(nb_cmd.HAChassisGroupAddCommand):
    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('HA_Chassis_Group', self.name).uuid
        except idlutils.RowNotFound:
            super().run_idl(txn)


class CreateSwitchWithLocalnetCommand(LsAddCommand):
    def __init__(self, api, name, network_name):
        super().__init__(api, name, may_exist=True)
        self.network_name = network_name

    def run_idl(self, txn):
        super().run_idl(txn)

        CreateLspLocalnetCommand(
            self.api, self.switch, self.network_name,
        ).run_idl(txn)


class CreateLspLocalnetCommand(LspAddCommand):
    def __init__(self, api, switch_name, network_name):
        localnet_lsp_name = get_lsp_localnet_name(switch_name)
        super().__init__(api, switch_name, localnet_lsp_name, may_exist=True)
        self.network_name = network_name

    def run_idl(self, txn):
        super().run_idl(txn)

        cmd.DbSetCommand(
            self.api,
            'Logical_Switch_Port',
            self.port,
            external_ids={constants.BGP_TAG: constants.BGP_LOCALNET},
            type='localnet',
            options={'network_name': self.network_name},
            addresses=['unknown']
        ).run_idl(txn)


class LrRouteAddCommand(nb_cmd.LrRouteAddCommand):
    def run_idl(self, txn):
        super().run_idl(txn)
        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup('Logical_Router_Static_Route', self.result)


class ReconcileRouterCommand(LrAddCommand):
    ROUTER_MAC_PREFIX = '00:00'

    def __init__(self, api, name):
        # We need to set policies and static_routes to empty list because IDL
        # won't have that set until the transaction is committed
        super().__init__(
            api, name, may_exist=True, policies=[], static_routes=[])
        mm = helpers.LrpMacManager.get_instance()
        mm.register_router(name, self.router_mac_prefix)

    def run_idl(self, txn):
        super().run_idl(txn)

        for key, value in self.options.items():
            self.result.setkey('options', key, value)

    @property
    def options(self):
        return {}

    @property
    def router_mac_prefix(self):
        base_mac = config.get_bgp_mac_base()
        return f'{base_mac}:{self.ROUTER_MAC_PREFIX}'


class ReconcileMainRouterCommand(ReconcileRouterCommand):
    ROUTER_MAC_PREFIX = '0b:96'

    @property
    def options(self):
        return {
            'dynamic-routing': 'true',
            'dynamic-routing-redistribute': 'connected-as-host,nat',
            'requested-tnl-key': config.get_bgp_router_tunnel_key(),
        }


class ReconcileChassisRouterCommand(ReconcileRouterCommand):
    def __init__(self, api, name, chassis):
        self.chassis = chassis
        super().__init__(api, name)

    @property
    def options(self):
        return {
            'chassis': self.chassis.name,
        }

    @property
    def router_mac_prefix(self):
        chassis_index = int(
            self.chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])
        base_mac = config.get_bgp_mac_base()

        # Two bytes for chassis
        hex_str = f"{chassis_index:0{4}x}"

        return f'{base_mac}:{hex_str[0:2]}:{hex_str[2:4]}'


class GetProviderLsCommand(cmd.BaseCommand):
    def __init__(self, api, provider_network_name):
        super().__init__(api)
        self.provider_network_name = provider_network_name

    def run_idl(self, txn):
        self.result = None

        lsps = _run_idl_command(
            cmd.DbFindCommand(
                self.api,
                'Logical_Switch_Port',
                ('type', '=', 'localnet'),
                ('options', '=', {'network_name': self.provider_network_name}),
                columns=['uuid', 'external_ids'],
            ),
            txn,
        )
        if not lsps:
            LOG.debug("Found no localnet port for provider network %s",
                      self.provider_network_name)
            return

        non_bgp_localnets = [
            lsp for lsp in lsps
            if lsp['external_ids'].get(
                constants.BGP_TAG) != constants.BGP_LOCALNET]

        if len(non_bgp_localnets) > 1:
            LOG.warning("Found multiple non-bgp localnets for provider "
                        "network %s", self.provider_network_name)
            return

        provider_localnet_lsp_uuid = non_bgp_localnets[0]['uuid']

        for ls in self.api.tables['Logical_Switch'].rows.values():
            if provider_localnet_lsp_uuid in [p.uuid for p in ls.ports]:
                self.result = ls
                return

        LOG.warning("Found no provider switch for provider network %s",
                    self.provider_network_name)


class IndexAllChassis(cmd.BaseCommand):
    def run_idl(self, txn):
        used_indexes = set()
        chassis_without_index = []

        for chassis in self.api.tables['Chassis'].rows.values():
            try:
                existing_index = int(
                    chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])
            except (KeyError, ValueError):
                chassis_without_index.append(chassis)
                continue

            used_indexes.add(int(existing_index))

        number_of_chassis = len(self.api.tables['Chassis'].rows)
        available_indexes = set(range(number_of_chassis)) - used_indexes
        for chassis in chassis_without_index:
            index = available_indexes.pop()
            chassis.setkey(
                'external_ids',
                constants.OVN_BGP_CHASSIS_INDEX_KEY,
                str(index),
            )

        self.result = [
            rowview.RowView(c)
            for c in self.api.tables['Chassis'].rows.values()]


class FullSyncBGPTopologyCommand(cmd.BaseCommand):
    def __init__(self, nb_api, sb_api, chassis):
        super().__init__(nb_api)
        self.chassis = chassis
        self.sb_api = sb_api

    def run_idl(self, txn):
        LOG.debug("BGP full sync topology started")
        self.reconcile_central(txn)
        self.reconcile_all_chassis(txn)
        LOG.debug("BGP full sync topology completed")

    def reconcile_all_chassis(self, txn):
        for chassis in self.chassis:
            ReconcileChassisCommand(self.api, self.sb_api, chassis).run_idl(txn)

    def reconcile_central(self, txn):
        router_name = config.get_main_router_name()
        ReconcileMainRouterCommand(
            self.api,
            router_name,
        ).run_idl(txn)

        ls_name = config.get_interconnect_switch_name()
        LsAddCommand(
            self.api,
            ls_name,
        ).run_idl(txn)

        mm = helpers.LrpMacManager.get_instance()
        lrp_mac = mm.get_mac_address(
            router_name, constants.LRP_MAIN_ROUTER_TO_INTERCONNECT_SWITCH)
        ConnectRouterToSwitchCommand(
            self.api,
            router_name,
            ls_name,
            lrp_mac=lrp_mac,
        ).run_idl(txn)


class ProvisionProviderNetworkCommand(cmd.BaseCommand):
    def __init__(self, api, provider_network_name, gateway_ip):
        super().__init__(api)
        self.provider_network_name = provider_network_name
        self.gateway_ip = gateway_ip

    def run_idl(self, txn):
        router_name = config.get_main_router_name()
        interconnect_switch_name = config.get_interconnect_switch_name()

        neutron_provider_switch = _run_idl_command(
            GetProviderLsCommand(self.api, self.provider_network_name),
            txn,
        )
        cmd.DbSetCommand(
            self.api,
            'Logical_Router_Port',
            get_lrp_name(router_name, interconnect_switch_name),
            networks=[self.gateway_ip],
        ).run_idl(txn)

        CreateLspLocalnetCommand(
            self.api,
            interconnect_switch_name,
            self.provider_network_name,
        ).run_idl(txn)

        connect_main_router_to_provider_switch(
            self.api,
            router_name,
            neutron_provider_switch.name,
            txn,
        )

        nb_cmd.LrRouteAddCommand(
            self.api,
            router_name,
            self.gateway_ip,
            self.gateway_ip.split('/')[0],
            port=get_lrp_name(router_name, interconnect_switch_name),
            may_exist=True,
        ).run_idl(txn)

        nb_cmd.LrRouteAddCommand(
            self.api,
            router_name,
            '0.0.0.0/0',
            self.gateway_ip.split('/')[0],
            may_exist=True,
        ).run_idl(txn)


class ConnectRouterToSwitchCommand(cmd.BaseCommand):
    def __init__(self, api, router_name, switch_name, lrp_mac, lrp_ip=None):
        super().__init__(api)
        self.router_name = router_name
        self.switch_name = switch_name
        self.lrp_mac = lrp_mac
        self.lrp_ip = lrp_ip

    def run_idl(self, txn):
        lrp_name = get_lrp_name(self.router_name, self.switch_name)
        if self.lrp_ip:
            networks = [self.lrp_ip]
        else:
            networks = []
        LrpAddCommand(
            self.api,
            self.router_name,
            lrp_name,
            mac=self.lrp_mac,
            networks=networks,
        ).run_idl(txn)

        lsp_name = get_lsp_name(self.switch_name, self.router_name)
        LspAddCommand(
            self.api,
            self.switch_name,
            lsp_name,
            addresses='router',
            type='router',
            options={'router-port': lrp_name},
        ).run_idl(txn)


class ConnectRouterToMainRouterCommand(cmd.BaseCommand):
    def __init__(self, api, router_name, chassis, hcg):
        super().__init__(api)
        self.router_name = router_name
        self.chassis = chassis
        self.hcg = hcg
        self.chassis_index = int(
            self.chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])

    def run_idl(self, txn):
        mm = helpers.LrpMacManager.get_instance()

        main_router_name = config.get_main_router_name()

        lrp_main = get_lrp_name(main_router_name, self.router_name)
        lrp_ch = get_lrp_name(self.router_name, main_router_name)

        lrp_main_mac = mm.get_mac_address(main_router_name, self.chassis_index)
        lrp_ch_mac = mm.get_mac_address(
            self.router_name, constants.LRP_CHASSIS_TO_MAIN_ROUTER)

        # TODO(jlibosva): Remove once we have IPv6 LLAs
        lrp_ch_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_CHASSIS_TO_MAIN_ROUTER)
        lrp_main_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)

        LrpAddCommand(
            self.api,
            self.router_name,
            lrp_ch,
            mac=lrp_ch_mac,
            networks=[lrp_ch_ip],
            peer=lrp_main
        ).run_idl(txn)

        LrpAddCommand(
            self.api,
            main_router_name,
            lrp_main,
            mac=lrp_main_mac,
            networks=[lrp_main_ip],
            peer=lrp_ch,
            ha_chassis_group=self.hcg,
            options={'dynamic-routing-maintain-vrf': 'true'},
        ).run_idl(txn)

        lrp_chassis_ip = helpers.InternalIpManager.get_ip(
            self.chassis_index, constants.LRP_CHASSIS_TO_MAIN_ROUTER)
        ls_interconnect_name = config.get_interconnect_switch_name()
        lrp_from_interconnect = get_lrp_name(
            main_router_name, ls_interconnect_name)

        nb_cmd.LrPolicyAddCommand(
            self.api,
            main_router_name,
            priority=10,
            match=f'inport==\"{lrp_from_interconnect}\" '
                  f'&& is_chassis_resident(\"cr-{lrp_main}\")',
            action='reroute',
            nexthops=[lrp_chassis_ip],
            may_exist=True,
        ).run_idl(txn)


class ReconcileChassisCommand(cmd.BaseCommand):
    def __init__(self, api, sb_api, chassis):
        super().__init__(api)
        self.sb_api = sb_api
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]
        self.chassis_index = int(
            self.chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])

    def run_idl(self, txn):
        hcg_name = f'bgp-hcg-{self.hostname}'
        hcg = _run_idl_command(HAChassisGroupAddCommand(
            self.api,
            hcg_name), txn)

        nb_cmd.HAChassisGroupAddChassisCommand(
            self.api,
            hcg,
            self.chassis.name, 10).run_idl(txn)

        router_name = f'bgp-lr-{self.hostname}'
        ReconcileChassisRouterCommand(
            self.api,
            router_name,
            self.chassis,
        ).run_idl(txn)

        # Connect chassis router to the main router
        ConnectRouterToMainRouterCommand(
            self.api,
            router_name,
            self.chassis,
            hcg,
        ).run_idl(txn)

        router_name = f'bgp-lr-{self.hostname}'
        bgp_peer_mapping = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        for peer_index, (network_name, (lrp_ip, peer_ip)) in enumerate(
                bgp_peer_mapping.items()):
            ReconcileChassisPeerCommand(
                self.api,
                self.sb_api,
                self.chassis,
                network_name,
                lrp_ip,
                peer_ip,
                peer_index,
            ).run_idl(txn)

class ReconcileChassisPeerCommand(cmd.BaseCommand):
    def __init__(
            self, api, sb_api, chassis, network_name, lrp_ip, peer_ip,
            peer_index):
        super().__init__(api)
        self.sb_api = sb_api
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]
        self.chassis_index = int(
            self.chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])
        self.network_name = network_name
        self.lrp_ip = lrp_ip
        self.peer_ip = peer_ip
        self.peer_index = peer_index

    @property
    def mac_manager(self):
        return helpers.LrpMacManager.get_instance()

    @property
    def router_name(self):
        return f'bgp-lr-{self.hostname}'

    @property
    def switch_name(self):
        return f'bgp-ls-{self.hostname}-{self.network_name}'

    def add_lrp_mac_to_chassis(self, lrp_mac):
        AddChassisLrpMacCommand(
            self.sb_api,
            self.chassis.name,
            self.network_name,
            lrp_mac,
        ).execute(check_error=True)

    def run_idl(self, txn):
        CreateSwitchWithLocalnetCommand(
            self.api,
            self.switch_name,
            self.network_name,
        ).run_idl(txn)

        lrp_mac = self.mac_manager.get_mac_address(
            self.router_name,
            constants.LRP_CHASSIS_ROUTER_TO_CHASSIS_SWITCH + self.peer_index)
        ConnectRouterToSwitchCommand(
            self.api,
            self.router_name,
            self.switch_name,
            lrp_mac=lrp_mac,
            lrp_ip=self.lrp_ip
        ).run_idl(txn)

        LOG.debug("XXX Adding route %s to %s", self.peer_ip, self.router_name)
        lr_route = _run_idl_command(LrRouteAddCommand(
            self.api,
            self.router_name,
            prefix='0.0.0.0/0',
            nexthop=self.peer_ip,
            ecmp=True,
            # port parameter is OVN output_port column
            port=get_lrp_name(self.router_name, self.switch_name),
            may_exist=True,
        ), txn)

        # TODO(jlibosva): Workaround for ovsdbapp bug, remove once fixed
        cmd.DbSetCommand(
            self.api,
            'Logical_Router_Static_Route',
            lr_route.uuid,
            external_ids={'foo': 'bar'},
        ).run_idl(txn)
        LOG.debug("XXX Added route to %s", self.router_name)

        main_router_lrp_ip = helpers.InternalIpManager.get_ip(
            self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)

        nb_cmd.LrPolicyAddCommand(
            self.api,
            self.router_name,
            priority=10,
            match=f'inport==\"{get_lrp_name(self.router_name, self.switch_name)}\"',
            action='reroute',
            nexthops=[main_router_lrp_ip],
            may_exist=True,
        ).run_idl(txn)

        self.add_lrp_mac_to_chassis(lrp_mac)


class GetChassisCommand(cmd.BaseCommand):
    def __init__(self, api, chassis):
        super().__init__(api)
        self.chassis = chassis

    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Chassis', self.chassis)
        except idlutils.RowNotFound:
            raise RuntimeError(f"Chassis {self.chassis} not found")


class DeleteChassisCommand(cmd.BaseCommand):
    def __init__(self, api, chassis):
        super().__init__(api)
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]

    def run_idl(self, txn):
        hcg_name= f'bgp-hcg-{self.hostname}'
        nb_cmd.HAChassisGroupDelChassisCommand(
            self.api,
            hcg_name,
            self.chassis.name).run_idl(txn)
        nb_cmd.HAChassisGroupDelCommand(
            self.api,
            hcg_name).run_idl(txn)

    def delete_provider_connection(self, txn):
        pass


class DeleteLrpConnectionCommand(nb_cmd.LrpDelCommand):
    def run_idl(self, txn):
        try:
            lrp = self.api.lookup('Logical_Router_Port', self.port)
        except idlutils.RowNotFound:
            LOG.debug("Lrp %s not found", self.port)

        lrp_peer_ports = cmd.DbFindCommand(
            self.api,
            'Logical_Router_Port',
            ('peer', '=', lrp.peer),
        ).run_idl(txn)
        for lrp_peer_port in lrp_peer_ports:
            nb_cmd.LrpDelCommand(
                self.api,
                lrp_peer_port).run_idl(txn)

        lsp_peer_ports = cmd.DbFindCommand(
                self.api,
                'Logical_Switch_Port',
                ('options', '=', {'router-port': lrp.peer}),
            ).run_idl(txn)
        for lsp_peer_port in lsp_peer_ports:
            nb_cmd.LspDelCommand(
                self.api,
                lsp_peer_port).run_idl(txn)


class AddChassisLrpMacCommand(cmd.BaseCommand):
    def __init__(self, api, chassis, network, lrp_mac):
        super().__init__(api)
        self.chassis = chassis
        self.network = network
        self.lrp_mac = lrp_mac

    def run_idl(self, txn):
        try:
            chassis = self.api.lookup('Chassis', self.chassis)
        except idlutils.RowNotFound:
            raise RuntimeError(f"Chassis {self.chassis} not found")

        new_lrp_mac_map = []
        try:
            lrp_mac_map = chassis.external_ids[
                constants.CHASSIS_BGP_LRP_MAC_MAP].split(',')
        except KeyError:
            lrp_mac_map = []

        for net_mac in lrp_mac_map:
            network, _ = net_mac.split(':', 1)
            if network != self.network:
                new_lrp_mac_map.append(net_mac)
        new_lrp_mac_map.append(f'{self.network}:{self.lrp_mac}')

        chassis.setkey('external_ids',
            constants.CHASSIS_BGP_LRP_MAC_MAP,
            ','.join(new_lrp_mac_map))