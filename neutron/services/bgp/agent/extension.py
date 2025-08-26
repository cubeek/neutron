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
from neutron.services.bgp.agent import exceptions as exc

LOG = log.getLogger(__name__)

LOCALHOST_ADDRESSES = ['127.0.0.1', '::1']
LOOPBACK_DEVICE = 'lo'


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


class Bridge(object):
    def __init__(self, bgp_agent_api, name):
        self.bgp_agent_api = bgp_agent_api
        self.name = name
        self.ovs_bridge = ovs_lib.OVSBridge(name)

    @property
    def ovs_idl(self):
        return self.bgp_agent_api.agent_api.ovs_idl

    @property
    def sb_idl(self):
        return self.bgp_agent_api.agent_api.sb_idl

    def _apply_flows_as_bundle(self, flows):
        """Apply multiple OpenFlow rules as a bundle using temporary file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.flows',
                                        prefix='bgp_', delete=True) as f:
            f.write("delete\n")
            for flow in flows:
                f.write(f"{flow}\n")
            f.flush()

            LOG.debug("Applying %d BGP flows as bundle to bridge %s",
                      len(flows), self.name)

            self.ovs_bridge.run_ofctl("add-flows", ["--bundle", f.name])


class BGPBridge(Bridge):
    @property
    def bridge_ips(self):
        return self.bgp_agent_api.devices_with_ips[self.name]

    def get_bridge_patch_port_ofport(self):
        """Return the ofport of the patch port of the given bridge"""
        for iface in self.bridge_ifaces():
            if iface['type'] == 'patch':
                return iface['ofport']
        raise ValueError(
            f"Expected 1 patch port for bridge {self.name}")

    def bridge_ifaces(self):
        ifaces = self.ovs_bridge.get_iface_name_list()
        return self.ovs_idl.db_list(
            'Interface', ifaces, if_exists=True).execute(check_error=True)

    def get_bridge_nic_ofport(self):
        """Return the ofport of the NIC of the given bridge"""
        for iface in self.bridge_ifaces():
            if iface['type'] not in ('patch', 'internal'):
                return iface['ofport']
        raise exc.BridgeNicException(f"Expected a NIC for bridge {self.name}")

    def get_bgp_connection_tuple(self):
        """Get the peer IPs of the chassis in the SB table"""
        found_peers = find_bgp_connections(self.bridge_ips)
        LOG.debug("Found BGP connection peers: %s", found_peers)

        peer_connections = None
        if len(found_peers) > 1:
            LOG.warning("Found multiple connections from BGP bridge %s, using"
                        " IPv4", self.name)
            peer_connections = [conn for conn in found_peers
                                if netaddr.IPAddress(conn[0]).version == 4][0]
        elif len(found_peers) == 0:
            LOG.warning("No connections found from BGP bridge %s", self.name)
        else:
            peer_connections = found_peers[0]

        if peer_connections:
            return peer_connections
        raise RuntimeError(f"No BGP connection found for {self.name}")

    def get_chassis_peer_connections_str(self):
        peer_connections = self.get_bgp_connection_tuple()
        return f'{self.name}:{peer_connections[0]}:{peer_connections[1]}'

    @property
    def host_ips(self):
        """Loopback IPs and the BGP peer ip"""
        ips = self.bgp_agent_api.devices_with_ips[self.name]
        if LOOPBACK_DEVICE in self.bgp_agent_api.devices_with_ips:
            ips.extend(self.bgp_agent_api.devices_with_ips[LOOPBACK_DEVICE])
        LOG.debug("XXX host IPs for %s: %s", self.name, ips)
        return ips

    @utils.throttler()
    def configure_flows(self):
        """Add BGP-specific OpenFlow rules using bundle from temporary file"""
        # The resulting openflows rules that will be written to a temporary
        # file and applied to the bridge.
        LOG.debug("XXX configuring BGP bridge flows for %s", self.name)
        flows = []
        try:
            nic_ofport = self.get_bridge_nic_ofport()
        except exc.BridgeNicException:
            LOG.warning("No NIC found for %s, clearing flows", self.name)
            self.ovs_bridge.run_ofctl("del-flows", [])
            return

        # Direct traffic meant for the host IPs
        for host_ip in self.host_ips:
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
        chassis_name = ovsdb.get_own_chassis_name(self.ovs_idl)
        ext_ids = self.sb_idl.db_get(
            'Chassis', chassis_name,
            'external_ids').execute(check_error=True)
        try:
            lrp_mac_map = ext_ids[constants.CHASSIS_BGP_LRP_MAC_MAP].split(',')
        except KeyError:
            lrp_mac_map = []
        for lrp_mac in lrp_mac_map:
            chassis_bridge_name, lrp_mac = lrp_mac.split(':', 1)
            if chassis_bridge_name == self.name:
                patch_port_ofport = self.get_bridge_patch_port_ofport()
                flows.append(
                    f"priority=80,in_port={nic_ofport},"
                    f"actions=mod_dl_dst:{lrp_mac},output:{patch_port_ofport}")

        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s",
                      self.name, e)


class ConnectingBridge(Bridge):
    def configure_flows(self):
        """The method ensures the right flows for connecting BGP and Neutron."""
        LOG.debug("XXX Configuring connecting bridge flows for %s", self.name)
        ports = self.ovs_bridge.get_bridge_patch_ports_ofports()
        if len(ports) != 2:
            LOG.debug("Expected 2 ports for %s, got %d, clearing flows",
                      self.name, len(ports))
            self.ovs_bridge.run_ofctl("del-flows", [])
            return
        patch_port_ofport = ports[0]
        other_port_ofport = ports[1]

        if not patch_port_ofport or not other_port_ofport:
            LOG.debug("No patch ports found for %s, clearing flows",
                      self.name)
            self.ovs_bridge.run_ofctl("del-flows", [])
            return

        flows = [
            f"priority=100,in_port={patch_port_ofport},"
            f"actions=output:{other_port_ofport}",

            f"priority=100,in_port={other_port_ofport},"
            f"actions=output:{patch_port_ofport}",
        ]
        self._apply_flows_as_bundle(flows)


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
            events.CreateChassisEvent,
            events.UpdateChassisEvent,
        ]

    def start(self):
        self._is_started_event.clear()
        self.create_bgp_bridges()
        self.devices_with_ips = self._load_devices_with_ips()
        super().start()
        self._is_started_event.set()

    def get_connecting_bridge(self, name):
        if not hasattr(self, '_connecting_bridge'):
            self._connecting_bridge = ConnectingBridge(self, name)
        return self._connecting_bridge

    @property
    def bgp_bridges_names(self):
        return list(self.bgp_bridges.keys())

    @property
    def managed_devices_names(self):
        return ['lo'] + self.bgp_bridges_names

    def create_bgp_bridges(self):
        """Create a list of BGP bridges objects"""
        self.bgp_bridges = {
            name: BGPBridge(self, name)
            for name in self.agent_api.ovs_idl.db_get(
                'Open_vSwitch', '.', 'external_ids').execute(check_error=True)
            .get(constants.AGENT_BGP_PEER_BRIDGES, '').split(',')
        }

    def update_chassis_external_ids(self, external_ids):
        """Update the chassis external IDs"""
        LOG.debug("XXX updating chassis external IDs to %s", external_ids)
        self.agent_api.sb_idl.db_set(
            'Chassis', ovsdb.get_own_chassis_name(self.agent_api.ovs_idl),
            external_ids=external_ids).execute(check_error=True)

    def update_chassis_peer_connections(self):
        """Update the chassis external IDs with the peer connections"""
        peer_connections = [br.get_chassis_peer_connections_str()
                            for br in self.bgp_bridges.values()]
        self.update_chassis_external_ids({
            constants.CHASSIS_PEER_CONNECTIONS: ','.join(peer_connections)
        })

    def remove_bgp_bridge_mappings(self, bgp_peer_bridges, ovn_bridge_mappings):
        new_mappings = [m for m in ovn_bridge_mappings
                        if m.split(':')[1] not in bgp_peer_bridges]
        ovsdb.set_ovn_bridge_mapping(
            self.agent_api.ovs_idl, new_mappings)

    def configure_bgp_bridge_mappings(
            self, bgp_peer_bridges, ovn_bridge_mappings):
        for bgp_bridge_name in bgp_peer_bridges:
            bgp_bridge_mapping = f'{bgp_bridge_name}:{bgp_bridge_name}'
            if bgp_bridge_mapping not in ovn_bridge_mappings:
                ovn_bridge_mappings.append(bgp_bridge_mapping)
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
                    devices_map.setdefault(device['name'], []).append(
                        netaddr.IPNetwork(device['cidr']))
        LOG.debug("XXX Loaded devices: %s", devices_map)
        return devices_map

    @property
    def host_ips(self):
        return [ip for ips in self.devices_with_ips.values() for ip in ips]

    def handle_patch_ports(self, bridge_name):
        if bridge_name in self.bgp_bridges:
            bridge = self.bgp_bridges[bridge_name]
        else:
            # If it's not a BGP bridge, it must be the bridge connecting OVN
            bridge = self.get_connecting_bridge(bridge_name)

        bridge.configure_flows()

