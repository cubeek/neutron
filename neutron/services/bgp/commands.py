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


class _LrAddCommand(nb_cmd.LrAddCommand):
    def run_idl(self, txn):
        try:
            self.result = self.api.lookup('Logical_Router', self.router)
        except idlutils.RowNotFound:
            super().run_idl(txn)
            self.result = self.api.lookup('Logical_Router', self.router)

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
