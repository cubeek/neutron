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
from ovsdbapp.backend.ovs_idl import rowview
from ovsdbapp.schema.ovn_northbound import commands as nb_cmd

from neutron.services.bgp import constants
from neutron.services.bgp import config
from neutron.services.bgp import helpers
from neutron.plugins.ml2.drivers.ovn.mech_driver.ovsdb import (
    commands as n_nb_commands)

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
    def __init__(self, api, router_name, lrp_name, mac, networks=None):
        networks = networks or []
        super().__init__(
            api, router_name, lrp_name, mac, networks, may_exist=True)

    def run_idl(self, txn):
        super().run_idl(txn)

        if isinstance(self.result, uuid.UUID):
            self.result = self.api.lookup('Logical_Router_Port', self.result)


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


class CreateRouterCommand(LrAddCommand):
    ROUTER_MAC_PREFIX = '00:00'

    def __init__(self, api, name):
        super().__init__(api, name, may_exist=True, policies=[])
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


class CreateMainRouterCommand(CreateRouterCommand):
    ROUTER_MAC_PREFIX = '0b:96'

    @property
    def options(self):
        return {
            'dynamic-routing': 'true',
            'dynamic-routing-redistribute': 'connected-as-host,nat',
            'requested-tnl-key': config.get_bgp_router_tunnel_key(),
        }


class CreateChassisRouterCommand(CreateRouterCommand):
    def __init__(self, api, name, chassis):
        self.chassis = chassis
        super().__init__(api, name)

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
    def __init__(self, nb_api, chassis):
        super().__init__(nb_api)
        self.chassis = chassis

    def run_idl(self, txn):
        LOG.debug("BGP full sync topology started")
        self.create_central(txn)
        self.create_all_chassis(txn)
        LOG.debug("BGP full sync topology completed")

    def create_all_chassis(self, txn):
        for chassis in self.chassis:
            CreateChassisCommand(self.api, chassis).run_idl(txn)

    def create_central(self, txn):
        router_name = config.get_main_router_name()
        CreateMainRouterCommand(
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
        ).run_idl(txn)

        nb_cmd.LrRouteAddCommand(
            self.api,
            router_name,
            '0.0.0.0/0',
            self.gateway_ip.split('/')[0],
        ).run_idl(txn)

class ConnectRouterToSwitchCommand(cmd.BaseCommand):
    def __init__(self, api, router_name, switch_name, lrp_mac):
        super().__init__(api)
        self.router_name = router_name
        self.switch_name = switch_name
        self.lrp_mac = lrp_mac

    def run_idl(self, txn):
        lrp_name = get_lrp_name(self.router_name, self.switch_name)
        LrpAddCommand(
            self.api,
            self.router_name,
            lrp_name,
            mac=self.lrp_mac,
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

        lrp_mac = mm.get_mac_address(
            self.router_name, constants.LRP_CHASSIS_TO_MAIN_ROUTER)
        main_router_name = config.get_main_router_name()

        lrp_chassis = get_lrp_name(self.router_name, main_router_name)
        # TODO(jlibosva): Remove once we have IPv6 LLAs
        lrp_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_CHASSIS_TO_MAIN_ROUTER)
        LrpAddCommand(
            self.api,
            self.router_name,
            lrp_chassis,
            mac=lrp_mac,
            networks=[lrp_ip],
        ).run_idl(txn)

        lrp_mac = mm.get_mac_address(main_router_name, self.chassis_index)
        lrp_main = get_lrp_name(main_router_name, self.router_name)
        lrp_ip = helpers.InternalIpManager.get_ip_cidr(
            self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)
        LrpAddCommand(
            self.api,
            main_router_name,
            lrp_main,
            mac=lrp_mac,
            networks=[lrp_ip],
        ).run_idl(txn)

        cmd.DbSetCommand(
            self.api,
            'Logical_Router_Port',
            lrp_chassis,
            peer=lrp_main,
        ).run_idl(txn)

        cmd.DbSetCommand(
            self.api,
            'Logical_Router_Port',
            lrp_main,
            peer=lrp_chassis,
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


class CreateChassisCommand(cmd.BaseCommand):
    def __init__(self, api, chassis):
        super().__init__(api)
        self.chassis = chassis
        self.hostname = self.chassis.hostname.split('.')[0]
        self.chassis_index = int(
            self.chassis.external_ids[constants.OVN_BGP_CHASSIS_INDEX_KEY])

    def run_idl(self, txn):
        hcg_name = f'bgp-hcg-{self.hostname}'
        hcg = _run_idl_command(nb_cmd.HAChassisGroupAddCommand(
            self.api,
            hcg_name,
            may_exist=True), txn)

        nb_cmd.HAChassisGroupAddChassisCommand(
            self.api,
            hcg,
            self.chassis.name, 10).run_idl(txn)

        router_name = f'bgp-lr-{self.hostname}'
        CreateChassisRouterCommand(
            self.api,
            router_name,
            self.chassis,
        ).run_idl(txn)

        cmd.DbSetCommand(
            self.api,
            'Logical_Router',
            router_name,
            options={'chassis': self.chassis.name},
        ).run_idl(txn)

        # Connect chassis router to the main router
        ConnectRouterToMainRouterCommand(
            self.api,
            router_name,
            self.chassis,
            hcg,
        ).run_idl(txn)

        mm = helpers.LrpMacManager.get_instance()
        bgp_peer_mapping = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        for i, network_name in enumerate(
                helpers.get_chassis_provider_networks(self.chassis)):
            switch_name = f'bgp-ls-{self.hostname}-{network_name}'
            CreateSwitchWithLocalnetCommand(
                self.api,
                switch_name,
                network_name,
            ).run_idl(txn)

            lrp_mac = mm.get_mac_address(
                router_name, constants.LRP_CHASSIS_TO_MAIN_ROUTER + 1 + i)
            peer_ip = bgp_peer_mapping[network_name]
            ConnectRouterToSwitchCommand(
                self.api,
                router_name,
                switch_name,
                lrp_mac=lrp_mac,
            ).run_idl(txn)

            cmd.DbSetCommand(
                self.api,
                'Logical_Router_Port',
                get_lrp_name(router_name, switch_name),
                networks=[peer_ip],
            ).run_idl(txn)

            # TODO(jlibosva): There are two commands to add static route
            # 1. AddStaticRouteCommand
            # 2. LrRouteAddCommand
            # We need to decide which one to use
            n_nb_commands.AddStaticRouteCommand(
                self.api,
                router_name,
                ip_prefix='0.0.0.0/0',
                nexthop=helpers.get_ipv4_peer_address(peer_ip),
                output_port=get_lrp_name(router_name, switch_name),
            ).run_idl(txn)

            main_router_lrp_ip = helpers.InternalIpManager.get_ip(
                self.chassis_index, constants.LRP_MAIN_ROUTER_TO_CHASSIS)

            nb_cmd.LrPolicyAddCommand(
                self.api,
                router_name,
                priority=10,
                match=f'inport==\"{get_lrp_name(router_name, switch_name)}\"',
                action='reroute',
                nexthops=[main_router_lrp_ip],
                may_exist=True,
            ).run_idl(txn)