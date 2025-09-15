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

import threading

import netaddr
from oslo_log import log

from neutron.agent.linux import ip_lib
from neutron.agent.ovn.agent import ovsdb
from neutron.agent.ovn.extensions import extension_manager as ovn_ext_mgr
from neutron.services.bgp import constants
from neutron.services.bgp.agent import bridge
from neutron.services.bgp.agent import events

LOG = log.getLogger(__name__)

LOCALHOST_ADDRESSES = ['127.0.0.1', '::1']


class BGPAgentExtension(ovn_ext_mgr.OVNAgentExtension):
    def __init__(self):
        super().__init__()
        self.devices_with_ips = {}
        self.bgp_bridges = {}
        self._is_started_event = threading.Event()

    @property
    def name(self):
        return "BGP agent extension"

    def initialize(self, *args):
        super().initialize(*args)
        self._is_started_event.clear()

    @property
    def ovs_idl_events(self):
        return [
            events.CreateLocalOVSEvent,
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
            'Chassis'
        ]

    @property
    def sb_idl_events(self):
        return [
        ]

    def start(self):
        self._is_started_event.clear()
        self.load_bgp_bridges()
        # A map of bridge names to the IPs on the bridge
        # Example: {
        #     'br-eth2': [
        #         netaddr.IPNetwork('192.168.1.1/30'),
        #         netaddr.IPNetwork('10.0.3.7/32')
        #     ]
        # }
        self.devices_with_ips = self._load_devices_with_ips()
        super().start()
        self._is_started_event.set()

    def get_connecting_bridge(self, name):
        if not hasattr(self, '_connecting_bridge'):
            self._connecting_bridge = bridge.ConnectingBridge(self, name)
        return self._connecting_bridge

    @property
    def bgp_bridges_names(self):
        return list(self.bgp_bridges.keys())

    @property
    def managed_devices_names(self):
        return ['lo'] + self.bgp_bridges_names

    def load_bgp_bridges(self):
        """Create a list of BGP bridges objects"""
        self.bgp_bridges = {
            name: bridge.BGPBridge(self, name)
            for name in self.agent_api.ovs_idl.db_get(
                'Open_vSwitch', '.', 'external_ids').execute(check_error=True)
            .get(constants.AGENT_BGP_PEER_BRIDGES, '').split(',')
        }

    def configure_bgp_bridge_mappings(
            self, bgp_peer_bridges, ovn_bridge_mappings):
        for bgp_bridge_name in bgp_peer_bridges:
            bgp_bridge_mapping = f'{bgp_bridge_name}:{bgp_bridge_name}'
            if bgp_bridge_mapping not in ovn_bridge_mappings:
                ovn_bridge_mappings.append(bgp_bridge_mapping)
        LOG.debug("Setting OVN bridge mappings: %s", ovn_bridge_mappings)
        ovsdb.set_ovn_bridge_mapping(
            self.agent_api.ovs_idl, ovn_bridge_mappings)

    def configure_chassis_bgp_bridges(self):
        for bgp_bridge in self.bgp_bridges.values():
            bgp_bridge.configure_flows()

    def _load_devices_with_ips(self):
        """Load the devices with IPs

        Loads the bridges configured in OVS bgp-bridges and loopback addresses.
        The localhost addresses are skipped.
        """
        devices_map = {}
        devices = ip_lib.get_devices_with_ip(namespace=None)
        for device in devices:
            if device['name'] in self.managed_devices_names:
                ip = netaddr.IPNetwork(device['cidr'])
                if str(ip.ip) not in LOCALHOST_ADDRESSES:
                    devices_map.setdefault(device['name'], []).append(ip)
        LOG.debug("Loaded devices: %s", devices_map)
        return devices_map

    @property
    def host_ips(self):
        return [ip for ips in self.devices_with_ips.values() for ip in ips]

    def handle_patch_ports(self, bridge_name):
        if bridge_name in self.bgp_bridges:
            br = self.bgp_bridges[bridge_name]
        else:
            # If it's not a BGP bridge, it must be the bridge connecting OVN
            br = self.get_connecting_bridge(bridge_name)

        br.configure_flows()
