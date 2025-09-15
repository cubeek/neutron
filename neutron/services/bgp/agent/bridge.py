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

import socket
import tempfile

import netaddr
from oslo_log import log
import psutil

from neutron.agent.common import ovs_lib
from neutron.agent.ovn.agent import ovsdb
from neutron.common import utils
from neutron.services.bgp import constants
from neutron.services.bgp.agent import exceptions as exc

LOG = log.getLogger(__name__)

LOOPBACK_DEVICE = 'lo'


def get_bgp_protocol_port_number():
    try:
        # Get it from the operating system first
        return socket.getservbyname('bgp', 'tcp')
    except socket.error:
        # If not found, use the default port number
        return constants.BGP_PORT_NUMBER


def find_bgp_connections(source_ip_list):
    """
    Find all remote peer IPs connected to a specific local source IP.

    returns a list of tuples of (source_ip_cidr, peer_ip)
    """
    found_peers = set()

    source_ip_dict = {str(ip.ip): ip for ip in source_ip_list}

    bgp_port_number = get_bgp_protocol_port_number()

    all_connections = psutil.net_connections(kind='tcp')
    for conn in all_connections:
        # Established connection with our monitored source IP and either source
        # or destination port is BGP port
        if (conn.laddr and conn.status == 'ESTABLISHED' and
                conn.laddr.ip in source_ip_dict and
                conn.raddr and bgp_port_number in [
                    conn.raddr.port, conn.laddr.port]):
            found_peers.add(
                (str(source_ip_dict[conn.laddr.ip]), conn.raddr.ip))

    LOG.debug("Found BGP connection peers: %s", found_peers)
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
    """BGP Bridge

    The BGP bridge is the provider bridge that connects a chassis to a BGP
    physical interface connected to a BGP peer, typically a leaf switch.
    """

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

        peer_connections = None
        if len(found_peers) > 1:
            LOG.info("Found multiple connections from BGP bridge %s, preferring"
                     " IPv4", self.name)
            peer_connections = [conn for conn in found_peers
                                if netaddr.IPAddress(conn[0]).version == 4]
        elif len(found_peers) == 0:
            LOG.warning("No connections found from BGP bridge %s", self.name)
        else:
            peer_connections = found_peers[0]

        if peer_connections:
            return peer_connections
        raise RuntimeError(f"No BGP connection found for {self.name}")

    def get_chassis_peer_connections_str(self):
        peer_connections = self.get_bgp_connection_tuple()
        return f'{self.name};{peer_connections[0]};{peer_connections[1]}'

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
        LOG.debug("configuring BGP bridge flows for %s", self.name)
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
    """Connecting Bridge

    The connecting bridge is the bridge that connects the BGP logical topology
    to the Neutron OVN resources.
    """

    def configure_flows(self):
        """The method ensures the right flows for connecting BGP and Neutron
        """
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