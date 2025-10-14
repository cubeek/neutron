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
from neutron.agent.linux import ip_lib
from neutron.agent.ovn.extensions.bgp import exceptions as exc
from neutron.common import utils
from neutron.services.bgp import constants

LOG = log.getLogger(__name__)


def get_bgp_protocol_port_number():
    try:
        # Get it from the operating system first
        return socket.getservbyname('bgp', 'tcp')
    except OSError:
        # If not found, use the default port number
        return constants.BGP_PORT_NUMBER


def find_bgp_connections(source_ip_list):
    """Find all remote peer IPs connected to a specific local source IP.

    returns a list of tuples of strings (source_ip_cidr, peer_ip)
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


class Bridge:
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


class BGPChassisBridge(Bridge):
    """BGP Bridge

    The BGP bridge is the provider bridge that connects a chassis to a BGP
    physical interface connected to a BGP peer, typically a leaf switch.
    """

    def __init__(self, bgp_agent_api, name):
        super().__init__(bgp_agent_api, name)
        self.lrp_mac = None

    def __str__(self):
        return f"BGPChassisBridge(name={self.name}, ips={self.ips})"

    __repr__ = __str__

    def get_bridge_patch_port_ofport(self):
        """Return the ofport of the patch port of the given bridge"""
        patch_ports_ofports = [
            iface['ofport'] for iface in self.bridge_ifaces()
            if iface['type'] == 'patch']
        if len(patch_ports_ofports) != 1:
            raise exc.BridgePatchPortException(
                f"Expected 1 patch port for bridge {self.name}, "
                f"got {len(patch_ports_ofports)}")
        return patch_ports_ofports[0]

    @property
    def exists(self):
        return self.ovs_bridge.bridge_exists(self.name)

    def bridge_ifaces(self):
        ifaces = self.ovs_bridge.get_iface_name_list()
        if not ifaces:
            return []
        return self.ovs_idl.db_list(
            'Interface', ifaces, if_exists=True).execute(check_error=True)

    def get_bridge_nic_ofport(self):
        """Return the ofport of the NIC of the given bridge"""
        nics_ofports = []
        for iface in self.bridge_ifaces():
            # REVISIT(jlibosva): we can consider supporting OVS bonds too
            if iface['type'] not in ('patch', 'internal'):
                nics_ofports.append(iface['ofport'])
        if len(nics_ofports) != 1:
            raise exc.BridgeNicException(
                f"Expected 1 NIC for bridge {self.name}, "
                f"got {len(nics_ofports)}")
        return nics_ofports[0]

    @property
    def ips(self):
        return [netaddr.IPNetwork(ip['cidr'])
                for ip in ip_lib.get_devices_with_ip(
                    namespace=None, name=self.name)]

    def _get_lrp_flow(self, nic_ofport, patch_port_ofport):
        if not self.lrp_mac:
            LOG.debug("No LRP MAC map found for %s", self.name)
            return []

        LOG.debug(f"Adding a flow to direct data plane traffic to OVN "
                    f"from {nic_ofport} to {patch_port_ofport} using MAC"
                    f" {self.lrp_mac}")
        return [
            f"priority=80,in_port={nic_ofport},"
            f"actions=mod_dl_dst:{self.lrp_mac},output:{patch_port_ofport}"
        ]

    def _get_flows_for_patch_port(self, patch_port_ofport):
        return [
            f"priority=100,in_port={patch_port_ofport},"
            f"actions=NORMAL"
        ]

    def _get_flows_for_nic_port(self, nic_ofport):
            # Allow IPv6 link-local traffic
        flows = [f"priority=100,ipv6,in_port={nic_ofport},ipv6_dst=fe80::/64 "
                 f"actions=NORMAL"]

        # Direct traffic meant for the host IPs
        for host_ip in self.bgp_agent_api.host_ips:
            if host_ip.version == 4:
                flows.append(f"priority=100,ip,in_port={nic_ofport},"
                             f"nw_dst={host_ip.ip} actions=NORMAL")
            elif host_ip.version == 6:
                flows.append(f"priority=100,ipv6,in_port={nic_ofport},"
                             f"ipv6_dst={host_ip.ip} actions=NORMAL")

        return flows

    @utils.throttler()
    def configure_flows(self):
        """Add BGP-specific OpenFlow rules using bundle from temporary file"""
        # The resulting openflows rules that will be written to a temporary
        # file and applied to the bridge.
        if not self.exists:
            LOG.warning("BGP bridge %s does not exist, skipping installing "
                        "flows", self.name)
            return

        LOG.debug("configuring BGP bridge flows for %s", self.name)
        # Allow ARP and ICMPv6
        flows = [
            "priority=100,arp actions=NORMAL",
            "priority=100,icmp6,icmp_type=133 actions=NORMAL",
            "priority=100,icmp6,icmp_type=134 actions=NORMAL",
            "priority=100,icmp6,icmp_type=135 actions=NORMAL",
            "priority=100,icmp6,icmp_type=136 actions=NORMAL",

            # Allow all other traffic
            "priority=0, actions=normal",
        ]

        try:
            nic_ofport = self.get_bridge_nic_ofport()
        except exc.BridgeNicException:
            LOG.info("No NIC found for %s, skipping flows", self.name)
            nic_ofport = None
        else:
            flows.extend(self._get_flows_for_nic_port(nic_ofport))

        try:
            patch_port_ofport = self.get_bridge_patch_port_ofport()
        except exc.BridgePatchPortException:
            LOG.info(
                "No patch port found for %s, skipping patch_port and "
                "LRP MAC map flows", self.name)
        else:
            flows.extend(self._get_flows_for_patch_port(patch_port_ofport))
            if nic_ofport:
                flows.extend(self._get_lrp_flow(nic_ofport, patch_port_ofport))

        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s",
                      self.name, e)

    @utils.throttler()
    def configure_flows_for_patch_port(self, patch_port_ofport):
        LOG.debug("XXX configuring BGP flows for patch port %s", patch_port_ofport)
        flows = self._get_flows_for_patch_port(patch_port_ofport)
        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s",
                      self.name, e)

    @utils.throttler()
    def configure_flows_for_nic_port(self, nic_ofport):
        LOG.debug("XXX configuring BGP flows for NIC port %s", nic_ofport)
        flows = self._get_flows_for_nic_port(nic_ofport)
        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s",
                      self.name, e)

    def get_bgp_connection_tuple(self):
        """Get the peer IPs of the chassis in the SB table"""
        found_peers = find_bgp_connections(self.ips)

        peer_connections = None
        if len(found_peers) > 1:
            LOG.info("Found multiple connections from BGP bridge %s, "
                     "preferring IPv4", self.name)
            peer_connections = [conn for conn in found_peers
                                if netaddr.IPAddress(conn[0]).version == 4]
        elif len(found_peers) == 0:
            LOG.warning("No connections found from BGP bridge %s", self.name)
        else:
            peer_connections = found_peers[0]

        if peer_connections:
            return peer_connections
        raise exc.NoBGPConnectionException(
            f"No BGP connection found for {self.name}")

    def get_chassis_peer_connections_str(self):
        peer_connections = self.get_bgp_connection_tuple()
        return f'{self.name};{peer_connections[0]};{peer_connections[1]}'
