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

import tempfile
import threading

import netaddr
from oslo_log import log
import psutil

from neutron.agent.common import ovs_lib
from neutron.agent.linux import ip_lib
from neutron.agent.ovn.agent import ovsdb
from neutron.agent.ovn.extensions import extension_manager as ovn_ext_mgr
from neutron.common import utils
from neutron.services.bgp import constants
from neutron.services.bgp.agent import events

LOG = log.getLogger(__name__)


def get_bgp_bridge_names(ovs_idl):
    """Get the names of the BGP bridges from the OVSDB"""
    ext_ids = ovs_idl.db_get(
        'Open_vSwitch', '.', 'external_ids').execute(check_error=True)
    return ext_ids.get(constants.AGENT_BGP_PEER_BRIDGES, '').split(',')


def find_bgp_connections(source_ip_list):
    """
    Finds all remote peer IPs connected to a specific local source IP.
    """
    found_peers = set()

    source_ip_list = [str(ip.ip) for ip in source_ip_list]

    all_connections = psutil.net_connections(kind='tcp')
    for conn in all_connections:
        if conn.laddr and conn.status == 'ESTABLISHED':
            if conn.laddr.ip in source_ip_list:
                if conn.raddr:
                    found_peers.add((conn.laddr.ip, conn.raddr.ip))
            elif conn.raddr.ip in source_ip_list:
                if conn.laddr:
                    found_peers.add((conn.raddr.ip, conn.laddr.ip))

    return list(found_peers)


class BGPAgentExtension(ovn_ext_mgr.OVNAgentExtension):
    def __init__(self):
        super().__init__()
        self.managed_devices = {}
        self.bgp_bridges = []
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
            events.UpdateLocalOVSEvent,
            events.PatchPortEvent,
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
            events.UpdateChassisEvent,
        ]

    def start(self):
        self._is_started_event.clear()
        self.bgp_bridges = get_bgp_bridge_names(self.agent_api.ovs_idl)
        self.managed_devices = self._load_devices()
        super().start()
        self._is_started_event.set()

    @property
    def managed_devices_names(self):
        return ['lo'] + self.bgp_bridges

    def remove_bgp_bridge_mappings(self, bgp_peer_bridges, ovn_bridge_mappings):
        new_mappings = [m for m in ovn_bridge_mappings
                        if m.split(':')[1] not in bgp_peer_bridges]
        ovsdb.set_ovn_bridge_mapping(
            self.agent_api.ovs_idl, new_mappings)

    def configure_bgp_bridge_mappings(
            self, bgp_peer_bridges, ovn_bridge_mappings):
        self._ensure_bgp_bridge_configured_in_ovn(
            bgp_peer_bridges, ovn_bridge_mappings)

    def _ensure_bgp_bridge_configured_in_ovn(
            self, bgp_peer_bridges, ovn_bridge_mappings):
        chassis_peer_connections = list()
        for bgp_bridge_name in bgp_peer_bridges:
            bgp_bridge_mapping = f'{bgp_bridge_name}:{bgp_bridge_name}'
            if bgp_bridge_mapping not in ovn_bridge_mappings:
                ovn_bridge_mappings.append(bgp_bridge_mapping)
            peer_connection = self.get_sb_chassis_peer_connections(bgp_bridge_name)
            if peer_connection:
                chassis_peer_connections.append(peer_connection)
            self.configure_bgp_bridge_flows(bgp_bridge_name)
        ovsdb.set_ovn_bridge_mapping(
            self.agent_api.ovs_idl, ovn_bridge_mappings)
        self.agent_api.sb_idl.db_set(
            'Chassis', ovsdb.get_own_chassis_name(self.agent_api.ovs_idl),
            external_ids={constants.CHASSIS_PEER_CONNECTIONS: ','.join(chassis_peer_connections)}
        ).execute(check_error=True)

    @utils.throttler()
    def configure_bgp_bridge_flows(self, bridge_name):
        """Add BGP-specific OpenFlow rules using bundle from temporary file"""
        # The resulting openflows rules that will be written to a temporary
        # file and applied to the bridge.
        bridge = ovs_lib.OVSBridge(bridge_name)
        flows = []
        nic_ofport = ovsdb.get_bridge_nic_ofport(
            self.agent_api.ovs_idl, bridge.br_name)

        # Direct traffic meant for the host IPs
        for host_ip in self.managed_devices[bridge.br_name]:
            if host_ip.version == 4:
                flows.append(f"priority=100,ip,in_port={nic_ofport},"
                             f"nw_dst={host_ip.ip} actions=NORMAL")
            elif host_ip.version == 6:
                flows.append(f"priority=100,ipv6,in_port={nic_ofport},"
                             f"ipv6_dst={host_ip.ip} actions=NORMAL")

        # Allow ARP and ICMPv6
        flows.extend([
            "priority=100,arp actions=NORMAL",
            "priority=100,icmp6,icmp_type=133 actions=NORMAL",
            "priority=100,icmp6,icmp_type=134 actions=NORMAL",
            "priority=100,icmp6,icmp_type=135 actions=NORMAL",
            "priority=100,icmp6,icmp_type=136 actions=NORMAL",

            # Allow IPv6 link-local traffic
            f"priority=100,ipv6,in_port={nic_ofport},ipv6_dst=fe80::/64 "
            "actions=NORMAL",

            # Allow all other traffic
            "priority=0, actions=normal"
        ])

        # We need to get the MAC of the LRP linked to this bridge so we can
        # steer the incoming traffic to OVN, the server stores it to the
        # Chassis table
        chassis_name = ovsdb.get_own_chassis_name(self.agent_api.ovs_idl)
        ext_ids = self.agent_api.sb_idl.db_get(
            'Chassis', chassis_name,
            'external_ids').execute(check_error=True)
        try:
            lrp_mac_map = ext_ids[constants.CHASSIS_BGP_LRP_MAC_MAP].split(',')
        except KeyError:
            lrp_mac_map = []
        for lrp_mac in lrp_mac_map:
            chassis_bridge_name, lrp_mac = lrp_mac.split(':')
            if chassis_bridge_name == bridge_name:
                patch_port_ofport = ovsdb.get_bridge_patch_port_ofport(
                    self.agent_api.ovs_idl, chassis_bridge_name)
                flows.append(
                    f"priority=80,in_port={nic_ofport},"
                    f"actions=mod_dl_dst:{lrp_mac},output:{patch_port_ofport}")

        try:
            self._apply_flows_as_bundle(bridge, flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s",
                      bridge_name, e)

    def _apply_flows_as_bundle(self, bridge, flows):
        """Apply multiple OpenFlow rules as a bundle using temporary file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.flows',
                                        prefix='bgp_', delete=True) as f:
            f.write("delete\n")
            for flow in flows:
                f.write(f"{flow}\n")
            f.flush()

            LOG.debug("Applying %d BGP flows as bundle to bridge %s",
                      len(flows), bridge.br_name)

            bridge.run_ofctl("add-flows", ["--bundle", f.name])

    def _load_devices(self):
        devices_map = {}
        devices = ip_lib.get_devices_with_ip(namespace=None)
        for device in devices:
            if device['name'] in self.managed_devices_names:
                devices_map.setdefault(device['name'], []).append(
                    netaddr.IPNetwork(device['cidr']))
        LOG.debug("XXX Loaded devices: %s", devices_map)
        return devices_map

    def get_sb_chassis_peer_connections(self, bridge_name):
        """Configure the peer IPs of the chassis in the SB table"""
        LOG.debug("XXX Configuring chassis peer connections for %s", bridge_name)
        chassis_peer_ips = self.managed_devices[bridge_name]
        found_peers = find_bgp_connections(chassis_peer_ips)
        LOG.debug("XXX Found peers: %s", found_peers)

        peer_connections = None
        if len(found_peers) > 1:
            LOG.warning("Found multiple connections from BGP bridge %s, using"
                        " IPv4", bridge_name)
            peer_connections = [conn for conn in found_peers
                                if netaddr.IPAddress(conn[0]).version == 4][0]
        elif len(found_peers) == 0:
            LOG.warning("No connections found from BGP bridge %s", bridge_name)
        else:
            peer_connections = found_peers[0]

        if peer_connections:
            conn_str = (f'{bridge_name}:{peer_connections[0]}:'
                        f'{peer_connections[1]}')
            return conn_str

    def handle_patch_port(self, port_name):
        LOG.debug("XXX Handling patch port %s", port_name)
        bridge_name = self.agent_api.ovs_idl.iface_to_br(
            port_name).execute(check_error=True)
        br_int = self.agent_api.ovs_idl.db_get(
            'Open_vSwitch', '.', 'external_ids').execute(check_error=True).get(
                'ovn-bridge'
            )
        LOG.debug("XXX Bridge name: %s, br_int: %s", bridge_name, br_int)

        if bridge_name in self.bgp_bridges:
            self.configure_bgp_bridge_flows(bridge_name)
        elif br_int == bridge_name:
            # Do not touch the integration bridge
            pass
        else:
            # This must be the bridge connecting OVN and BGP
            self.configure_connecting_bridge_flows(bridge_name)

    @utils.throttler()
    def configure_connecting_bridge_flows(self, bridge_name):
        bridge = ovs_lib.OVSBridge(bridge_name)
        ports = bridge.get_bridge_patch_ports_ofports()
        if len(ports) != 2:
            LOG.debug("Expected 2 ports for %s, got %d, clearing flows",
                      bridge_name, len(ports))
            bridge.run_ofctl("del-flows", [bridge_name])
            return
        patch_port_ofport = ports[0]
        other_port_ofport = ports[1]
        flows = [
            f"priority=100,in_port={patch_port_ofport},actions=output:{other_port_ofport}",
            f"priority=100,in_port={other_port_ofport},actions=output:{patch_port_ofport}",
        ]
        self._apply_flows_as_bundle(bridge, flows)
