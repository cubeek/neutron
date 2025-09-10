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

from neutron.conf.services import bgp as bgp_config
from neutron.services.bgp import constants
from neutron.services.bgp import exceptions
from neutron.services.bgp import helpers

LOG = log.getLogger(__name__)


def _run_idl_command(cmd, txn):
    cmd.run_idl(txn)
    return cmd.result


def get_lrp_name(from_name, to_name):
    return f'bgp-lrp-{from_name}-to-{to_name}'


def get_lsp_name(from_name, to_name):
    return f'bgp-lsp-{from_name}-to-{to_name}'


def get_lsp_localnet_name(switch_name):
    return f'bgp-lsp-{switch_name}-localnet'


def get_hcg_name(hostname):
    return f'bgp-hcg-{hostname}'


def get_chassis_router_name(hostname):
    return f'bgp-lr-{hostname}'


def get_chassis_index(chassis):
    try:
        return int(chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])
    except (KeyError, ValueError):
        msg = (f"Chassis {chassis.name} has no index, cannot create "
               "chassis resources")
        LOG.error(msg)
        # TODO(jlibosva): Use resource types for custom exceptions
        raise exceptions.ReconcileError(msg)


class _LsAddCommand(nb_cmd.LsAddCommand):
    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Switch', self.switch)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            self.result = self.api.lookup('Logical_Switch', self.switch)

        self.set_columns(self.result, **self.columns)


class _LrAddCommand(nb_cmd.LrAddCommand):
    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Router', self.router)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            self.result = self.api.lookup('Logical_Router', self.router)

        self.set_columns(self.result, **self.columns)


class _LspAddCommand(nb_cmd.LspAddCommand):
    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Switch_Port', self.port)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            self.result = self.api.lookup('Logical_Switch_Port', self.port)

        self.set_columns(self.result, **self.columns)


class _LrpAddCommand(nb_cmd.LrpAddCommand):
    def __init__(
            self, api, router_name, lrp_name, mac, networks=None, **kwargs):
        networks = networks or []
        super().__init__(api, router_name, lrp_name, mac, networks, **kwargs)

    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Router_Port', self.port)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            self.result = self.api.lookup('Logical_Router_Port', self.port)
        self.result.mac = self.mac
        self.result.networks = self.networks
        self.result.peer = self.peer
        self.set_columns(self.result, **self.columns)


class _HAChassisGroupAddCommand(nb_cmd.HAChassisGroupAddCommand):
    def run_idl(self, txn):
        try:
            hcg = self.api.lookup('HA_Chassis_Group', self.name)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            hcg = self.api.lookup('HA_Chassis_Group', self.name)

        self.set_columns(hcg, **self.columns)

        self.result = hcg.uuid


class CreateSwitchWithLocalnetCommand(_LsAddCommand):
    def __init__(self, api, name, network_name):
        super().__init__(api, name, may_exist=True)
        self.network_name = network_name

    def run_idl(self, txn):
        super().run_idl(txn)

        CreateLspLocalnetCommand(
            self.api, self.switch, self.network_name,
        ).run_idl(txn)


class CreateLspLocalnetCommand(_LspAddCommand):
    def __init__(self, api, switch_name, network_name):
        self.localnet_lsp_name = get_lsp_localnet_name(switch_name)
        super().__init__(
            api, switch_name, self.localnet_lsp_name, may_exist=True)
        self.network_name = network_name

    def run_idl(self, txn):
        super().run_idl(txn)

        lsp = self.api.lookup('Logical_Switch_Port', self.localnet_lsp_name)

        columns = {
            'type': 'localnet',
            'options': {'network_name': self.network_name},
            'addresses': ['unknown'],
        }

        self.set_columns(lsp, **columns)


class _LrRouteAddCommand(nb_cmd.LrRouteAddCommand):
    def run_idl(self, txn):
        super().run_idl(txn)
        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup(
                'Logical_Router_Static_Route', self.result)


class ReconcileRouterCommand(_LrAddCommand):
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
        base_mac = bgp_config.get_bgp_mac_base()
        return f'{base_mac}:{self.ROUTER_MAC_PREFIX}'


class ReconcileMainRouterCommand(ReconcileRouterCommand):
    ROUTER_MAC_PREFIX = '0b:96'

    def __init__(self, api):
        super().__init__(api, self.get_name())

    @property
    def options(self):
        return {
            'dynamic-routing': 'true',
            'dynamic-routing-redistribute': 'connected-as-host,nat',
            'requested-tnl-key': bgp_config.get_bgp_router_tunnel_key(),
        }

    @classmethod
    def get_name(cls):
        return bgp_config.get_main_router_name()


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
        chassis_index = get_chassis_index(self.chassis)
        base_mac = bgp_config.get_bgp_mac_base()

        # Two bytes for chassis
        hex_str = f"{chassis_index:0{4}x}"

        return f'{base_mac}:{hex_str[0:2]}:{hex_str[2:4]}'


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
        _LrpAddCommand(
            self.api,
            self.router_name,
            lrp_name,
            mac=self.lrp_mac,
            networks=networks,
        ).run_idl(txn)

        lsp_name = get_lsp_name(self.switch_name, self.router_name)
        _LspAddCommand(
            self.api,
            self.switch_name,
            lsp_name,
            addresses='router',
            type='router',
            options={'router-port': lrp_name},
        ).run_idl(txn)


class ConnectChassisRouterToSwitchCommand(ConnectRouterToSwitchCommand):
    def __init__(
            self, api, router_name, switch_name, lrp_mac, lrp_ip,
            network_name):
        super().__init__(api, router_name, switch_name, lrp_mac, lrp_ip)
        self.network_name = network_name

    def run_idl(self, txn):
        super().run_idl(txn)
        lrp_name = get_lrp_name(self.router_name, self.switch_name)
        cmd.DbSetCommand(
            self.api,
            'Logical_Router_Port',
            lrp_name,
            external_ids={
                constants.BGP_CHASSIS_NETWORK_NAME: self.network_name,
                constants.BGP_CHASSIS_LRP_MAC: self.lrp_mac,
            },
        ).run_idl(txn)


class ConnectRouterToMainRouterCommand(cmd.BaseCommand):
    def __init__(self, api, router_name, chassis, hcg):
        super().__init__(api)
        self.router_name = router_name
        self.hcg = hcg
        self.chassis_index = get_chassis_index(chassis)

    def validate_prerequisites(self):
        for router_name in [
                bgp_config.get_main_router_name(), self.router_name]:
            try:
                self.api.lookup('Logical_Router', router_name)
            except idlutils.RowNotFound:
                raise exceptions.ReconcileError(
                    f"Router {router_name} not found")

    def run_idl(self, txn):
        self.validate_prerequisites()

        mm = helpers.LrpMacManager.get_instance()

        main_router_name = bgp_config.get_main_router_name()

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

        _LrpAddCommand(
            self.api,
            self.router_name,
            lrp_ch,
            mac=lrp_ch_mac,
            networks=[lrp_ch_ip],
            peer=lrp_main
        ).run_idl(txn)

        _LrpAddCommand(
            self.api,
            main_router_name,
            lrp_main,
            mac=lrp_main_mac,
            networks=[lrp_main_ip],
            peer=lrp_ch,
            ha_chassis_group=self.hcg,
            options={'dynamic-routing-maintain-vrf': 'true'},
        ).run_idl(txn)


class ReconcileChassisCommand(cmd.BaseCommand):
    """Reconcile all BGP components for a chassis

    The command reconciles the chassis router and all its configured peer
    connections based on the configured BGP bridges on the given chassis. It
    creates a logical switch with a localnet port connected to the BGP bridge,
    creates routes in and out on the router and connects the router to the main
    router with a peer connection.
    """

    def __init__(self, api, sb_api, chassis):
        super().__init__(api)
        self.sb_api = sb_api
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]
        self.chassis_index = get_chassis_index(self.chassis)

    def run_idl(self, txn):
        hcg_name = get_hcg_name(self.hostname)
        hcg = _run_idl_command(_HAChassisGroupAddCommand(
            self.api,
            hcg_name), txn)

        nb_cmd.HAChassisGroupAddChassisCommand(
            self.api,
            hcg,
            self.chassis.name, 10).run_idl(txn)

        router_name = get_chassis_router_name(self.hostname)
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

        router_name = get_chassis_router_name(self.hostname)
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
    """The command reconciles a BGP peer connection for a chassis

    The BGP peer connection is based on the BGP bridge chassis configuration.
    It creates a logical switch with a localnet port connected to the BGP
    bridge. All traffic is routed out to the localnet port but there is a
    policy based on the inport, so traffic coming from the main BGP router is
    rerouted with ECMP to the peer IP, that typically resides on the
    neighboring physical switch.
    """
    def __init__(
            self, api, sb_api, chassis, network_name, lrp_ip, peer_ip,
            peer_index):
        super().__init__(api)
        self.sb_api = sb_api
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]
        self.chassis_index = get_chassis_index(self.chassis)
        self.network_name = network_name
        self.lrp_ip = lrp_ip
        self.peer_ip = peer_ip
        self.peer_index = peer_index

    @property
    def mac_manager(self):
        return helpers.LrpMacManager.get_instance()

    @property
    def router_name(self):
        return get_chassis_router_name(self.hostname)

    @property
    def switch_name(self):
        return f'bgp-ls-{self.hostname}-{self.network_name}'

    def run_idl(self, txn):
        CreateSwitchWithLocalnetCommand(
            self.api,
            self.switch_name,
            self.network_name,
        ).run_idl(txn)

        lrp_mac = self.mac_manager.get_mac_address(
            self.router_name,
            constants.LRP_CHASSIS_ROUTER_TO_CHASSIS_SWITCH + self.peer_index)
        ConnectChassisRouterToSwitchCommand(
            self.api,
            self.router_name,
            self.switch_name,
            lrp_mac=lrp_mac,
            lrp_ip=self.lrp_ip,
            network_name=self.network_name,
        ).run_idl(txn)

        _run_idl_command(_LrRouteAddCommand(
            self.api,
            self.router_name,
            prefix='0.0.0.0/0',
            nexthop=self.peer_ip,
            ecmp=True,
            # port parameter is OVN output_port column
            port=get_lrp_name(self.router_name, self.switch_name),
            may_exist=True,
        ), txn)

        main_router_lrp_ip = helpers.InternalIpManager.get_ip(
            self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)

        match=f'inport==\"{get_lrp_name(self.router_name, self.switch_name)}\"'
        nb_cmd.LrPolicyAddCommand(
            self.api,
            self.router_name,
            priority=10,
            match=match,
            action='reroute',
            nexthops=[main_router_lrp_ip],
            may_exist=True,
        ).run_idl(txn)


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
            ReconcileChassisCommand(
                self.api, self.sb_api, chassis).run_idl(txn)

    def reconcile_central(self, txn):
        ReconcileMainRouterCommand(
            self.api,
        ).run_idl(txn)
