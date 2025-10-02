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

from neutron.common.ovn import constants as ovn_const
from neutron.common.ovn import utils as ovn_utils
from neutron.conf.plugins.ml2.drivers.ovn import ovn_conf
from neutron.conf.services import bgp as bgp_config
from neutron.services.bgp import commands
from neutron.services.bgp import constants
from neutron.services.bgp import events
from neutron.services.bgp import helpers
from neutron.services.bgp import ovn

LOG = log.getLogger(__name__)


class BGPTopologyReconciler:
    def __init__(self):
        self.nb_api = ovn.OvnNbIdl(
            ovn_conf.get_ovn_nb_connection(),
            self.nb_events).start(
                timeout=ovn_conf.get_ovn_ovsdb_timeout())
        self.nb_api.set_lock()

        self.sb_api = ovn.OvnSbIdl(
            ovn_conf.get_ovn_sb_connection(),
            self.sb_events).start(
                timeout=ovn_conf.get_ovn_ovsdb_timeout())

    def stop(self):
        self.nb_api.stop()
        self.sb_api.stop()

    def reset_connections(self):
        self.nb_api.restart_connection()
        self.sb_api.restart_connection()

    @property
    def resource_map(self):
        return {
            'reconcile': {
                constants.BGPReconcilerResource.CHASSIS_BGP_BRIDGES:
                    self.reconcile_chassis_bgp_bridges,
                constants.BGPReconcilerResource.PROVIDER_SWITCH:
                    self.reconcile_provider_switch,
                constants.BGPReconcilerResource.GATEWAY_IP_ROUTE:
                    self.reconcile_gateway_ip_route,
            },
            'delete': {
                constants.BGPReconcilerResource.PROVIDER_SWITCH:
                    self.delete_provider_switch,
                constants.BGPReconcilerResource.GATEWAY_IP_ROUTE:
                    self.delete_gateway_ip_route,
            },
        }

    @property
    def nb_events(self):
        return [
            events.ProviderSwitchCreatedEvent(self),
            events.ProviderSwitchDeletedEvent(self),
            events.GatewayIPRouteEvent(self),
        ]

    @property
    def sb_events(self):
        return [
            events.BGPChassisBridgesUpdateEvent(self),
        ]

    def full_sync(self):
        if not self.nb_api.ovsdb_connection.idl.is_lock_contended:
            LOG.info("Full BGP topology synchronization started")
            # First make sure all chassis are indexed
            commands.FullSyncBGPTopologyCommand(
                self.nb_api, self.sb_api).execute(check_error=True)
            LOG.info(
                "Full BGP topology synchronization completed successfully")
        else:
            LOG.info("Full BGP topology synchronization already in progress")

    def reconcile(self, action, resource, trigger):
        try:
            self.resource_map[action][resource](trigger)
        except KeyError:
            LOG.error("Resource %s not found in reconciler resource map",
                      resource)

    def reconcile_chassis_bgp_bridges(self, chassis):
        for bgp_bridge in helpers.get_chassis_bgp_bridges(chassis):
            commands.ReconcileChassisPeerCommand(
                self.nb_api,
                chassis,
                network_name=bgp_bridge,
            ).execute(check_error=True)

    def reconcile_provider_switch(self, switch):
        gw_ips = helpers.get_gw_ips(switch)
        commands.ReconcileNeutronSwitchCommand(
            self.nb_api,
            switch,
            gw_ips,
        ).execute(check_error=True)

    def delete_provider_switch(self, switch):
        commands.DeleteNeutronSwitchCommand(
            self.nb_api,
            switch,
        ).execute(check_error=True)

    def delete_gateway_ip_route(self, route):
        pass

    def reconcile_gateway_ip_route(self, route):
        LOG.debug("Reconciling gateway IP %s", route.nexthop)
        subnet_id = route.external_ids[ovn_const.OVN_SUBNET_EXT_ID_KEY]
        subnet = helpers.get_subnet(subnet_id)
        gw_ip = helpers.get_gw_ip(subnet)

        n_switch_name = ovn_utils.ovn_name(subnet['network_id'])
        n_switch = self.nb_api.lookup('Logical_Switch', n_switch_name)
        interconnect_switch_name = (
            helpers.get_provider_interconnect_switch_name(n_switch_name))
        main_router_name = bgp_config.get_main_router_name()
        lrp_name = helpers.get_lrp_name(
            main_router_name, interconnect_switch_name)
        with self.nb_api.transaction() as txn:
            txn.add(commands.ReconcileMainRouterRoutesForProviderCommand(
                self.nb_api,
                interconnect_switch_name,
                [gw_ip],
                related_switch=n_switch,
            ))
            txn.add(self.nb_api.db_set(
                'Logical_Router_Port',
                lrp_name,
                networks=[gw_ip]))
