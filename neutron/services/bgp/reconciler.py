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

import netaddr
from neutron_lib import context as n_context
from neutron_lib.api.definitions import provider_net
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log

from neutron.services.bgp import commands
from neutron.services.bgp import helpers
from neutron.services.bgp import resources

LOG = log.getLogger(__name__)


class BGPTopologyReconciler:

    def __init__(self, nb_ovn, sb_ovn, bgp_config):
        self.nb_ovn = nb_ovn
        self.sb_ovn = sb_ovn
        self.config = bgp_config

    @property
    def all_resources(self):
        neutron_plugin = directory.get_plugin()
        ctx = n_context.get_admin_context()
        external_net = helpers.get_neutron_external_network(neutron_plugin, ctx)
        external_net_physnet = external_net[provider_net.PHYSICAL_NETWORK]
        gw_ip = None
        prefixlen = None

        for subnet_id in external_net['subnets']:
            subnet = neutron_plugin.get_subnet(ctx, subnet_id)
            if subnet['ip_version'] == 4:
                gw_ip = subnet['gateway_ip']
                prefixlen = netaddr.IPNetwork(subnet['cidr']).prefixlen
                break

        if not gw_ip:
            # TODO (jlibosva): Make it a custom exception
            raise Exception("No IPv4 gateway found for external network")

        main_router = resources.MainRouter(resources.ResourceAction.CREATE)
        interconnect_switch = resources.InterconnectSwitch(
            resources.ResourceAction.CREATE,
            external_net_physnet,
        )

        bgp_resources = {
            main_router,
            interconnect_switch,
        }

        bgp_resources.add(
            resources.Connection(
                resources.ResourceAction.CREATE,
                interconnect_switch,
                main_router,
                network=f"{gw_ip}/{prefixlen}",
            )
        )

        for chassis_res in self._chassis_resources:
            bgp_resources.add(chassis_res)
            bgp_resources.add(
                resources.Connection(
                    resources.ResourceAction.CREATE,
                    chassis_res.router,
                    main_router)
            )

        return bgp_resources

    @property
    def _chassis_resources(self):
        for chassis in helpers.get_all_chassis(self.sb_ovn):
            yield resources.ChassisResource(
                resources.ResourceAction.CREATE, chassis)


#    def verify_entity_integrity(self, row, old=None):
#        """Verify integrity of a BGP entity after creation/update."""
#        entity_name = getattr(row, 'name', 'unknown')
#        entity_type = row._table.name
#
#        LOG.debug("Verifying integrity of BGP entity: %s (%s)",
#                  entity_name, entity_type)
#
#                # Check if entity has proper BGP ownership tag
#        external_ids = getattr(row, 'external_ids', {})
#        owner = external_ids.get(events.OWNER_KEY)
#        if owner != events.BGP_OWNER_TAG:
#            LOG.warning("BGP entity %s missing or incorrect ownership tag: %s",
#                       entity_name, owner)
#
        # Additional integrity checks can be added here
        # For example, checking required attributes, relationships, etc.

    def full_sync(self):
        LOG.info("Starting BGP topology full synchronization")
        commands.IndexAllChassis(self.sb_ovn).execute(check_error=True)

        self.reconcile_resources(self.all_resources)

    def reconcile_resources(self, resources):
        LOG.debug("Reconciling topology")
        failed_resources = set()
        for resource in resources:
            LOG.debug("Reconciling resource: %s", resource)
            try:
                resource.reconcile(self.nb_ovn)
            except Exception as e:
                LOG.exception("Error during BGP topology reconciliation of resource %s: %s", resource, e)
                failed_resources.add(resource)
            else:
                LOG.debug("BGP topology reconciliation of resource %s completed successfully", resource)
        if failed_resources:
            LOG.info("Reconciliation of resources %s failed, scheduling next reconciliation", failed_resources)
            # TODO (jlibosva): Schedule next reconciliation


























#    def _reconcile_interconnect_switch(self):
#        """Ensure the interconnect switch exists."""
#
#        neutron_plugin = directory.get_plugin()
#        ctx = n_context.get_admin_context()
#        external_net = helpers.get_neutron_external_network(neutron_plugin, ctx)
#        external_net_physnet = external_net[provider_net.PHYSICAL_NETWORK]
#
#        # TODO (jlibosva): Handle IPv6
#        gw_ip = None
#        prefixlen = None
#
#        for subnet_id in external_net['subnets']:
#            subnet = neutron_plugin.get_subnet(ctx, subnet_id)
#            if subnet['ip_version'] == 4:
#                gw_ip = subnet['gateway_ip']
#                prefixlen = netaddr.IPNetwork(subnet['cidr']).prefixlen
#                break
#
#        if not gw_ip:
#            # TODO (jlibosva): Make it a custom exception
#            raise Exception("No IPv4 gateway found for external network")
#
#        LOG.info("Creating interconnect switch: %s", self.config.interconnect_switch_name)
#        with self.nb_ovn.transaction(check_error=True) as txn:
#            txn.add(self.nb_ovn.ls_add(self.config.interconnect_switch_name, may_exist=True))
#            txn.add(self.nb_ovn.db_set('Logical_Switch', self.config.interconnect_switch_name,
#                                        ('external_ids', {constants.OWNER_KEY: constants.BGP_OWNER_TAG})))
#
#            # Add localnet port
#            localnet_port = f"lsp-{self.config.interconnect_switch_name}-localnet"
#            txn.add(self.nb_ovn.lsp_add(self.config.interconnect_switch_name, localnet_port, may_exist=True))
#            txn.add(self.nb_ovn.db_set(
#                'Logical_Switch_Port', localnet_port,
#                ('external_ids', {constants.OWNER_KEY: constants.BGP_OWNER_TAG}),
#                ('type', 'localnet'),
#                ('options', {'network_name': external_net_physnet}),
#                ('addresses', ['unknown'])))
#
#        # Connect to main BGP router
#        # The whole transaction fails if the BGP router does not exist yet
#        # but the router is attempted to be connected to the switch when creating the BGP router
#        with self.nb_ovn.transaction(check_error=False) as txn:
#            self._connect_router_to_switch(
#                txn,
#                self.config.bgp_router_name,
#                self.config.interconnect_switch_name,
#                '00:de:ad:10:00:00',
#                [f"{gw_ip}/{prefixlen}"])
#
#        LOG.info("Successfully created interconnect switch")
#
#    def _get_neutron_switch(self):
#        """Find the neutron switch connected to datacentre physical network."""
#        try:
#            # Find localnet port for datacentre network
#            localnet_ports = self.nb_ovn.db_find_rows(
#                'Logical_Switch_Port',
#                ('type', '=', 'localnet'),
#                ('options', '=', {'network_name': self.config.physical_network})
#            ).execute(check_error=True)
#
#            if localnet_ports:
#                # Find the switch that contains this localnet port
#                for port in localnet_ports:
#                    switches = self.nb_ovn.db_find_rows(
#                        'Logical_Switch',
#                        ('ports', '=', port.uuid)
#                    ).execute(check_error=True)
#
#                    if switches:
#                        switch_name = switches[0].name
#                        if switch_name != self.config.interconnect_switch_name:
#                            LOG.debug("Found neutron switch: %s", switch_name)
#                            return switch_name
#
#            LOG.warning("Could not find neutron switch, using fallback")
#            return "neutron-switch"  # Fallback name
#
#        except Exception as e:
#            LOG.error("Failed to find neutron switch: %s", e)
#            return "neutron-switch"  # Fallback name
#
#    def _reconcile_main_router(self):
#        """Ensure the main BGP router exists and is properly configured."""
#        router_name = self.config.bgp_router_name
#        interconnect_switch = self.config.interconnect_switch_name
#
#        neutron_plugin = directory.get_plugin()
#        ctx = n_context.get_admin_context()
#        external_net = self.get_neutron_external_network(neutron_plugin, ctx)
#
#        neutron_external_ls = self.nb_ovn.db_find_rows(
#            'Logical_Switch', ('name', '=', f"neutron-{external_net['id']}")).execute(check_error=True)
#
#
#        LOG.debug("Neutron external switch: %s", neutron_external_ls)
#        if not neutron_external_ls:
#            LOG.error("Neutron external switch not found")
#            return
#
#        neutron_external_ls = neutron_external_ls[0]
#        LOG.info("Creating main BGP router: %s", router_name)
#        with self.nb_ovn.transaction(check_error=True) as txn:
#            txn.add(self.nb_ovn.lr_add(router_name,  may_exist=True))
#            txn.add(self.nb_ovn.db_set(
#                'Logical_Router', router_name,
#                ('external_ids', {constants.OWNER_KEY: constants.BGP_OWNER_TAG}),
#                ('options', {
#                    'dynamic-routing': 'true',
#                    'dynamic-routing-redistribute': 'connected-as-host',
#                    'requested-tnl-key': self.config.bgp_router_tunnel_key
#                })
#            ))
#
#            # Connect to neutron switch
#            self._connect_router_to_switch(txn, router_name, interconnect_switch, '00:de:ad:10:00:00', [])
#            # Create unused connection to connect BGP router datapath with Neutron external switch
#            self._connect_router_to_switch(txn, router_name, neutron_external_ls.name, '00:de:ad:de:ad:00', [])
#        # Add default routes
#        public_subnet = self.config.public_subnet
#        txn.add(self.nb_ovn.lr_route_add(router_name, public_subnet,
#                                        gateway_ip, lrp_name))
#        txn.add(self.nb_ovn.lr_route_add(router_name, '0.0.0.0/0',
#                                        gateway_ip, None))

#    def _ensure_chassis_topology(self, chassis):
#        """Ensure per-chassis topology exists (router, switches, HA group)."""
#        chassis_name = chassis['hostname'].split('.')[0]  # Remove domain if present
#        rack_num = chassis['rack_num']
#        router_name = f"bgp-router-{chassis_name}"
#
#        try:
#            # Check if chassis router exists
#            router = self.nb_ovn.db_find_rows(
#                'Logical_Router', ('name', '=', router_name)).execute(check_error=True)
#
#            if not router:
#                LOG.info("Creating chassis topology for %s", chassis_name)
#                self._create_chassis_topology(chassis_name, rack_num, chassis['name'])
#            else:
#                LOG.debug("Chassis topology for %s already exists", chassis_name)
#
#        except Exception as e:
#            LOG.error("Failed to ensure chassis topology for %s: %s", chassis_name, e)
#            raise

#    def _create_chassis_topology(self, chassis_name, rack_num, chassis_uuid):
#        """Create complete topology for a single chassis."""
#        router_name = f"bgp-router-{chassis_name}"
#        bgp_router = self.config.bgp_router_name
#
#        with self.nb_ovn.transaction(check_error=True) as txn:
#            # Create HA chassis group
#            ha_group_name = f"ha-group-{chassis_name}"
#            txn.add(self.nb_ovn.ha_chassis_group_add(
#                ha_group_name,
#                external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}))
#            txn.add(self.nb_ovn.ha_chassis_group_add_chassis(ha_group_name, chassis_uuid, 10))
#
#            # Create chassis router
#            txn.add(self.nb_ovn.lr_add(router_name, external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}))
#            txn.add(self.nb_ovn.db_set('Logical_Router', router_name,
#                                     ('options', {'chassis': chassis_uuid})))
#
#            # Create peer connection to main BGP router
#            lrp_chassis = f"lrp-{router_name}-to-{bgp_router}"
#            lrp_main = f"lrp-{bgp_router}-to-{router_name}"
#            chassis_ip = f"169.254.{rack_num}.1/30"
#            main_ip = f"169.254.{rack_num}.2/30"
#
#            txn.add(self.nb_ovn.lrp_add(router_name, lrp_chassis,
#                                      f"00:de:ad:00:0{rack_num}:00", chassis_ip,
#                                      external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}))
#            txn.add(self.nb_ovn.lrp_add(bgp_router, lrp_main,
#                                      f"00:de:ad:00:1{rack_num}:00", main_ip,
#                                      external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}))
#
#            # Set up peering
#            txn.add(self.nb_ovn.db_set('Logical_Router_Port', lrp_chassis,
#                                     ('peer', [lrp_main])))
#            txn.add(self.nb_ovn.db_set('Logical_Router_Port', lrp_main,
#                                     ('peer', [lrp_chassis])))
#
#            # Set HA chassis group for main router port
#            ha_group = self.nb_ovn.db_find_rows(
#                'HA_Chassis_Group', ('name', '=', ha_group_name)).execute()[0]
#            txn.add(self.nb_ovn.db_set('Logical_Router_Port', lrp_main,
#                                     ('ha_chassis_group', [ha_group.uuid])))
#            txn.add(self.nb_ovn.db_set('Logical_Router_Port', lrp_main,
#                                     ('options', {'dynamic-routing-maintain-vrf': 'true'})))
#
#            # Create chassis switches and connect them
#            for eth_num in [2, 3]:
#                switch_name = f"ls-{chassis_name}-eth{eth_num}"
#                network_base = 100 + 62 + eth_num  # 164 or 165
#                router_ip = f"{network_base}.{rack_num}.2"
#                gateway_ip = f"{network_base}.{rack_num}.1"
#
#                # Create switch
#                txn.add(self.nb_ovn.ls_add(switch_name, external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}))
#
#                # Connect router to switch
#                self._connect_router_to_switch(txn, router_name, switch_name,
#                                             f"00:de:ad:2{rack_num}:0{eth_num}:00", router_ip)
#
#                # Add localnet port to switch
#                localnet_port = f"lsp-{switch_name}-localnet"
#                txn.add(self.nb_ovn.lsp_add(switch_name, localnet_port, external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}, may_exist=True))
#                txn.add(self.nb_ovn.db_set('Logical_Switch_Port', localnet_port,
#                                         ('type', 'localnet')))
#                txn.add(self.nb_ovn.db_set('Logical_Switch_Port', localnet_port,
#                                         ('options', {'network_name': f"net-eth{eth_num}"})))
#                txn.add(self.nb_ovn.db_set('Logical_Switch_Port', localnet_port,
#                                         ('addresses', ['unknown'])))
#
#                # Add default route via this interface (ECMP)
#                lrp_name = f"lrp-{router_name}-to-{switch_name}"
#                txn.add(self.nb_ovn.lr_route_add(router_name, '0.0.0.0/0',
#                                               gateway_ip, lrp_name, policy='src-ip'))
#
#            # Add route to public subnet via main BGP router
#            public_subnet = self.config.public_subnet
#            next_hop = f"169.254.{rack_num}.2"
#            txn.add(self.nb_ovn.lr_route_add(router_name, public_subnet, next_hop, lrp_chassis))
#
#            # Add routing policy to main BGP router
#            policy_match = f'inport=="lrp-{bgp_router}-to-{self.config.interconnect_switch_name}" && is_chassis_resident("cr-{lrp_main}")'
#            reroute_nexthop = f"169.254.{rack_num}.1"
#            txn.add(self.nb_ovn.lr_policy_add(bgp_router, 10, policy_match, 'reroute', reroute_nexthop))
#
#    def _connect_router_to_switch(self, txn, router_name, switch_name, mac_addr, ip_addr):
#        """Helper method to connect a router to a switch."""
#        lrp_name = f"lrp-{router_name}-to-{switch_name}"
#        lsp_name = f"lsp-{switch_name}-to-{router_name}"
#
#        LOG.debug("Connecting router %s to switch %s", router_name, switch_name)
#        txn.add(self.nb_ovn.lrp_add(router_name, lrp_name, mac_addr, ip_addr,
#                                    external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}, may_exist=True))
#
#        txn.add(self.nb_ovn.lsp_add(switch_name, lsp_name, external_ids={constants.OWNER_KEY: constants.BGP_OWNER_TAG}, may_exist=True))
#        txn.add(self.nb_ovn.db_set('Logical_Switch_Port', lsp_name,
#                                   ('type', 'router'),
#                                   ('options', {'router-port': lrp_name}),
#                                   ('addresses', ['router'])))