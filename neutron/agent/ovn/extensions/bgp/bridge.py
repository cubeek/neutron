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
import time

from oslo_log import log

from neutron.agent.common import ovs_lib
from neutron.agent.ovn.extensions.bgp import exceptions as exc
from neutron.common.ovn import constants as ovn_const
from neutron.services.bgp import constants

LOG = log.getLogger(__name__)


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

    def __str__(self):
        return f"BGPChassisBridge(name={self.name})"

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

    @property
    def exists(self):
        return self.ovs_bridge.bridge_exists(self.name)

    def bridge_ifaces(self):
        ifaces = self.ovs_bridge.get_iface_name_list()
        if not ifaces:
            return []
        return self.ovs_idl.db_list(
            'Interface', ifaces, if_exists=True).execute(check_error=True)

    def _get_lrp_mac(self):
        ext_ids = {constants.LRP_NETWORK_NAME_EXT_ID_KEY: self.name}
        port_bindings = self.sb_idl.db_find_rows(
            'Port_Binding',
            ('type', '=', ovn_const.PB_TYPE_L3GATEWAY),
            ('external_ids', '=', ext_ids)).execute(
                check_error=True)
        for pb in port_bindings:
            if (pb.external_ids.get(
                    constants.LRP_NETWORK_NAME_EXT_ID_KEY) == self.name):
                return pb.mac[0].split(' ', 1)[0]

        LOG.debug("LRP MAC does not exist yet for %s", self.name)
        return None

    def _get_bridge_ofports_per_type(self, type):
        return [
            iface['ofport'] for iface in self.bridge_ifaces()
            if iface['type'] == type]

    def _get_bridge_patch_port_ofport(self):
        patch_ports_ofports = self._get_bridge_ofports_per_type('patch')
        if len(patch_ports_ofports) != 1:
            LOG.debug("The patch port for bridge %s does not exist yet",
                      self.name)
            return None
        return patch_ports_ofports[0]

    def _get_bridge_nic_ofport(self):
        # REVISIT(jlibosva): we can consider supporting OVS bonds too
        nics_ofports = self._get_bridge_ofports_per_type('')
        if len(nics_ofports) != 1:
            raise exc.BridgeNicException(
                f"Expected 1 NIC for bridge {self.name}, got "
                f"{len(nics_ofports)}")
        return nics_ofports[0]

    def _check_requirements_met(self):
        if not self.exists:
            LOG.error("BGP bridge %s does not exist", self.name)
            return

        if not self.nic_ofport:
            LOG.error("No NIC port found for %s", self.name)
            return False

        if not self.patch_port_ofport:
            LOG.error("No patch port found for %s", self.name)
            return False

        if not self.lrp_mac:
            LOG.error("No LRP MAC found for %s", self.name)
            return False

        return True

    def _get_flows_for_lrp(self):
        if not self.lrp_mac:
            LOG.error("No LRP MAC map found for %s", self.name)
            return []

        if not self.nic_ofport:
            LOG.error("No NIC port found for %s", self.name)
            return []

        if not self.patch_port_ofport:
            LOG.error("No patch port found for %s", self.name)
            return []

        LOG.debug(f"Adding a flow to direct data plane traffic to OVN "
                  f"from {self.nic_ofport} to {self.patch_port_ofport} using "
                  f"MAC {self.lrp_mac}")
        return [
            f"priority=80,in_port={self.nic_ofport},"
            f"actions=mod_dl_dst:{self.lrp_mac},"
            f"output:{self.patch_port_ofport}"
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
        flows = [f"priority=100,ipv6,in_port={self.nic_ofport},"
                 f"ipv6_dst=fe80::/64 actions=NORMAL"]

        # Direct traffic meant for the host IPs
        for host_ip in self.bgp_agent_api.host_ips:
            if host_ip.version == 4:
                flows.append(f"priority=100,ip,in_port={self.nic_ofport},"
                             f"nw_dst={host_ip.ip} actions=NORMAL")
            elif host_ip.version == 6:
                flows.append(f"priority=100,ipv6,in_port={self.nic_ofport},"
                             f"ipv6_dst={host_ip.ip} actions=NORMAL")

        return flows

    def configure_flows(self):
        # The resulting openflows rules that will be written to a temporary
        # file and applied to the bridge.
        if not self._check_requirements_met():
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
        flows.extend(self._get_flows_for_lrp())

        try:
            self._apply_flows_as_bundle(flows)
        except Exception as e:
            LOG.error("Failed to configure BGP flows on bridge %s: %s: %s",
                      self.name, e, flows)
