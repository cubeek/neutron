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
from oslo_log import log

from neutron.agent.linux import ip_lib
from neutron.agent.ovn.agent import ovsdb
from neutron.agent.ovn.extensions.bgp import bridge
from neutron.agent.ovn.extensions.bgp import events
from neutron.agent.ovn.extensions.bgp import exceptions as exc
from neutron.agent.ovn.extensions import extension_manager as ovn_ext_mgr
from neutron.services.bgp import constants

LOG = log.getLogger(__name__)

LOCALHOST_ADDRESSES = ('127.0.0.1', '::1')


class BGPAgentExtension(ovn_ext_mgr.OVNAgentExtension):
    def __init__(self):
        super().__init__()
        # A map of bridge names to the IPs on the bridge
        # Example: {
        #     'br-eth2': [
        #         netaddr.IPNetwork('192.168.1.1/30'),
        #         netaddr.IPNetwork('10.0.3.7/32')
        #     ]
        # }
        self.bgp_bridges = {}

    @property
    def name(self):
        return "BGP agent extension"

    @property
    def ovs_idl_events(self):
        return [
            events.CreateLocalOVSEvent,
            events.UpdateLocalOVSEvent,
            events.NewBgpBridgeEvent,
            events.BgpBridgePortEvent,
        ]

    @property
    def nb_idl_tables(self):
        return []

    @property
    def nb_idl_events(self):
        return []

    @property
    def sb_idl_tables(self):
        return [
            'Chassis',
            'Port_Binding',
        ]

    @property
    def sb_idl_events(self):
        return [
            events.CreateChassisEvent,
            events.PortBindingLrpMacEvent,
        ]

    def configure_bgp_bridge_mappings(self, ovn_bridge_mappings):
        LOG.debug("Setting OVN bridge mappings: %s", ovn_bridge_mappings)
        ovsdb.set_ovn_bridge_mapping(
            self.agent_api.ovs_idl, ovn_bridge_mappings)

    def post_connect_ovs_idl(self):
        self.load_bgp_bridges()

    def load_bgp_bridges(self):
        """Create a list of BGP bridges objects"""
        self.bgp_bridges = {
            name: bridge.BGPChassisBridge(self, name)
            for name in self.agent_api.ovs_idl.db_get(
                'Open_vSwitch', '.', 'external_ids').execute(check_error=True)
            .get(constants.AGENT_BGP_PEER_BRIDGES, '').split(',')
            if name
        }

    @property
    def host_ips(self):
        host_ips = self.loopback_ips
        for bgp_bridge in self.bgp_bridges.values():
            host_ips.extend(bgp_bridge.ips)
        return host_ips

    @property
    def loopback_ips(self):
        cidrs = [netaddr.IPNetwork(dev['cidr'])
                 for dev in ip_lib.get_devices_with_ip(
                 namespace=None, name=ip_lib.LOOPBACK_DEVNAME)]
        return [cidr for cidr in cidrs
                if str(cidr.ip) not in LOCALHOST_ADDRESSES]

    def configure_all_chassis_bgp_bridges(self):
        for bgp_bridge in self.bgp_bridges.values():
            bgp_bridge.configure_flows()

    def update_chassis_external_ids(self, external_ids):
        """Update the chassis external IDs"""
        self.agent_api.sb_idl.db_set(
            'Chassis', ovsdb.get_own_chassis_name(self.agent_api.ovs_idl),
            external_ids=external_ids).execute(check_error=True)

    def update_chassis_peer_connections(self):
        """Update the chassis external IDs with the peer connections"""
        peer_connections = []
        for br in self.bgp_bridges.values():
            try:
                peer_connections.append(br.get_chassis_peer_connections_str())
            except exc.NoBGPConnectionException as e:
                LOG.error(
                    "Failed to find BGP connection for the BGP bridge %s: %s",
                    br.name, e)
                continue
        if not peer_connections:
            LOG.error("No BGP connection found for the chassis")
            return
        self.update_chassis_external_ids({
            constants.CHASSIS_PEER_CONNECTIONS: ','.join(peer_connections)
        })

    def configure_chassis_bgp_bridge(self, network_name, lrp_mac):
        try:
            bridge = self.bgp_bridges[network_name]
        except KeyError:
            LOG.warning("No BGP bridge found for network %s", network_name)
            return
        bridge.lrp_mac = lrp_mac
        bridge.configure_flows()
