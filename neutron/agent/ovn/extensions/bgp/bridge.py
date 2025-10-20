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
import threading
import time

import netaddr
from oslo_log import log
import psutil

from neutron.agent.common import ovs_lib
from neutron.agent.linux import ip_lib
from neutron.agent.ovn.extensions.bgp import exceptions as exc
from neutron.common.ovn import constants as ovn_const
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

            self.ovs_bridge.run_ofctl(
                "add-flows", ["--bundle", f.name])


class BGPChassisBridge(Bridge):
    """BGP Bridge

    The BGP bridge is the provider bridge that connects a chassis to a BGP
    physical interface connected to a BGP peer, typically a leaf switch.
    """

    def __init__(self, bgp_agent_api, name):
        super().__init__(bgp_agent_api, name)
        self.lrp_mac = self._get_lrp_mac()
        self.patch_port_ofport = self._get_bridge_patch_port_ofport()
        # the NIC exists but may not have an ofport yet
        self._nic_ofport = self._get_bridge_nic_ofport()
        self.stop_bgp_discovery_event = threading.Event()

    def __str__(self):
        return f"BGPChassisBridge(name={self.name}, ips={self.ips})"

    __repr__ = __str__

    @property
    def nic_ofport(self):
        # The NIC existed when the bridge was created but did not have an
        # ofport assigned yet
        if self._nic_ofport == []:
            for i in range(11):
                self._nic_ofport = self._get_bridge_nic_ofport()
                if self._nic_ofport == []:
                    time.sleep(0.1)
                    continue
                break
            else:
                LOG.error("ofport for NIC for bridge %s has not been set",
                          self.name)
        return self._nic_ofport

    def _get_lrp_mac(self):
        ext_ids = {constants.BGP_CHASSIS_NETWORK_NAME: self.name}
        port_bindings = self.sb_idl.db_find_rows(
            'Port_Binding',
            ('type', '=', ovn_const.PB_TYPE_L3GATEWAY),
            ('external_ids', '=', ext_ids)).execute(
                check_error=True)
        for pb in port_bindings:
            if (pb.chassis and
                    pb.chassis[0].name == self.bgp_agent_api.chassis_name):
                return pb.mac[0].split(' ', 1)[0]

        return None

    def _get_bridge_ofports_per_type(self, type):
        return [
            iface['ofport'] for iface in self.bridge_ifaces()
            if iface['type'] == type]

    def _get_bridge_patch_port_ofport(self):
        patch_ports_ofports = self._get_bridge_ofports_per_type('patch')
        if len(patch_ports_ofports) != 1:
            return None
        return patch_ports_ofports[0]

    def _get_bridge_nic_ofport(self):
        # REVISIT(jlibosva): we can consider supporting OVS bonds too
        nics_ofports = self._get_bridge_ofports_per_type('')
        if len(nics_ofports) != 1:
            raise exc.BridgeNicException(
                f"Expected 1 NIC for bridge {self.name}, got "
                f"{len(nics_ofports)}")
        LOG.debug("XXX NIC %s", nics_ofports)
        return nics_ofports[0]

    def discover_bgp_peer_in_thread(self):
        """Spawn a thread to discover the BGP peer IP"""
        bgp_discovery_thread = threading.Thread(
            target=self._discover_bgp_peer,
            name=f"BGP-Peer-Discovery-{self.name}"
        )
        bgp_discovery_thread.daemon = True
        LOG.debug("XXX Starting BGP peer discovery thread for bridge %s",
                  self.name)
        bgp_discovery_thread.start()

    def _discover_bgp_peer(self):
        """Poll until BGP peer connection is found, then signal and exit"""
        LOG.debug("XXX BGP peer discovery polling started for bridge %s",
                  self.name)

        while not self.stop_bgp_discovery_event.is_set():
            if not self.exists:
                LOG.debug("Bridge %s no longer exists, stopping peer "
                          "discovery", self.name)
                return

            try:
                # This will raise NoBGPConnectionException if not found
                peer_connection = self.get_chassis_peer_connections_str()
            except exc.NoBGPConnectionException:
                pass
            else:
                LOG.info("XXX Discovered BGP peer for bridge %s: %s",
                        self.name, peer_connection)

                # Update chassis external IDs with peer connection
                # This must happen BEFORE signaling, as it triggers the creation
                # of logical switches and patch ports by OVN controller
                try:
                    self.bgp_agent_api.add_chassis_peer_connections(
                        peer_connection)
                    LOG.debug("XXX Updated chassis peer connections for "
                              "bridge %s", self.name)
                except Exception as e:
                    LOG.error("Failed to update chassis peer connections for "
                             "bridge %s: %s", self.name, e)
                    # Continue anyway - the set_bgp_session_event() will allow
                    # configuration to proceed

                return

            self.stop_bgp_discovery_event.wait(1.0)

        LOG.debug("XXX BGP peer discovery stopped for bridge %s", self.name)

    @property
    def exists(self):
        return self.ovs_bridge.bridge_exists(self.name)

    def bridge_ifaces(self):
        ifaces = self.ovs_bridge.get_iface_name_list()
        if not ifaces:
            return []
        return self.ovs_idl.db_list(
            'Interface', ifaces, if_exists=True).execute(check_error=True)

    @property
    def ips(self):
        return [netaddr.IPNetwork(ip['cidr'])
                for ip in ip_lib.get_devices_with_ip(
                    namespace=None, name=self.name)]

    def _get_lrp_flow(self):
        if not self.lrp_mac:
            LOG.error("No LRP MAC map found for %s", self.name)
            return []

        if not self.nic_ofport:
            LOG.error("No NIC port found for %s", self.name)
            return []

        LOG.debug(f"Adding a flow to direct data plane traffic to OVN "
                  f"from {self.nic_ofport} to {self.patch_port_ofport} using "
                  f"MAC {self.lrp_mac}")
        return [
            f"priority=80,in_port={self.nic_ofport},"
            f"actions=mod_dl_dst:{self.lrp_mac},output:{self.patch_port_ofport}"
        ]

    def _get_flows_for_patch_port(self):
        if not self.patch_port_ofport:
            LOG.error("Attempting to configure flows for patch port, but no "
                      "patch port found for %s", self.name)
            return []
        return [
            f"priority=100,in_port={self.patch_port_ofport},"
            f"actions=NORMAL"
        ]

    def _get_flows_for_nic_port(self):
        if not self.nic_ofport:
            LOG.error("Attempting to configure flows for NIC port, but no "
                      "NIC port found for %s", self.name)
            return []
            # Allow IPv6 link-local traffic
        flows = [f"priority=100,ipv6,in_port={self.nic_ofport},ipv6_dst=fe80::/64 "
                 f"actions=NORMAL"]

        # Direct traffic meant for the host IPs
        for host_ip in self.bgp_agent_api.host_ips:
            if host_ip.version == 4:
                flows.append(f"priority=100,ip,in_port={self.nic_ofport},"
                             f"nw_dst={host_ip.ip} actions=NORMAL")
            elif host_ip.version == 6:
                flows.append(f"priority=100,ipv6,in_port={self.nic_ofport},"
                             f"ipv6_dst={host_ip.ip} actions=NORMAL")

        return flows

    @utils.throttler()
    def configure_flows(self):
        LOG.debug("XXX configuring BGP bridge flows for %s", self.name)
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


        flows.extend(self._get_flows_for_nic_port())
        flows.extend(self._get_flows_for_patch_port())
        flows.extend(self._get_lrp_flow())

        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s: %s",
                      self.name, e, flows)

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
