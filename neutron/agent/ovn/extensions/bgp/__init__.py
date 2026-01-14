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
from neutron.agent.ovn.extensions.bgp import commands
from neutron.agent.ovn.extensions.bgp import events
from neutron.agent.ovn.extensions import extension_manager as ovn_ext_mgr

LOG = log.getLogger(__name__)

LOCALHOST_ADDRESSES = ['127.0.0.1', '::1']


class BGPAgentExtension(ovn_ext_mgr.OVNAgentExtension):
    def __init__(self):
        super().__init__()
        # A map of bridge names to the bridge object
        # Example: {
        #     'br-eth1': BGPChassisBridge('br-eth1'),
        #     'br-eth2': BGPChassisBridge('br-eth2'),
        # }
        self.bgp_bridges = {}

    @property
    def name(self):
        return "BGP agent extension"

    @property
    def ovs_idl_events(self):
        return [
            events.CreateLocalOVSEvent,
            events.NewBgpBridgeEvent,
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
            'Port_Binding',
        ]

    @property
    def sb_idl_events(self):
        return [
            events.PortBindingLrpMacEvent,
        ]

    @property
    def chassis_name(self):
        return ovsdb.get_own_chassis_name(self.agent_api.ovs_idl)

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

    def create_bgp_bridge(self, bridge_name):
        bgp_bridge = bridge.BGPChassisBridge(self, bridge_name)
        self.bgp_bridges[bridge_name] = bgp_bridge
        return bgp_bridge

    def set_chassis_bgp_bridges(self, bridge_name_list):
        commands.SetChassisBgpBridgesCommand(
            self.agent_api.sb_idl,
            self.chassis_name,
            bridge_name_list
        ).execute(check_error=True)

    def watch_patch_port_created_event(self, bgp_bridge):
        # Check the patch port doesn't exist on the bridge
        patch_ports_ofports = (
            bgp_bridge.ovs_bridge.get_bridge_patch_ports_ofports())

        if not patch_ports_ofports:
            LOG.debug("Waiting for a patch port creation on bridge %s",
                      bgp_bridge.name)
            event_handler = self.agent_api.ovs_idl.idl.notify_handler
            event = events.BGPBridgePatchPortCreatedEvent(
                self.agent_api, bgp_bridge.name)
            event_handler.watch_event(event)

            # Check the patch port again in case it was created in the meantime
            patch_ports_ofports = (
                bgp_bridge.ovs_bridge.get_bridge_patch_ports_ofports())
            if patch_ports_ofports:
                LOG.debug(
                    "The patch port was created in the meantime on bridge %s "
                    "with ofport %d, removing the onetime event from the "
                    "queue.", bgp_bridge.name, patch_ports_ofports[0])
                event_handler.unwatch_event(event)
                bgp_bridge.patch_port_ofport = patch_ports_ofports[0]
                bgp_bridge.configure_flows()
        else:
            LOG.debug("The BGP bridge %s already has a patch port with ofport"
                      " %d", bgp_bridge.name, patch_ports_ofports[0])
            bgp_bridge.patch_port_ofport = patch_ports_ofports[0]
            bgp_bridge.configure_flows()
