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


from oslo_log import log

from neutron.agent.common import ovs_lib
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


class BGPChassisBridge(Bridge):
    """BGP Bridge

    The BGP bridge is the provider bridge that connects a chassis to a BGP
    physical interface connected to a BGP peer, typically a leaf switch.
    """
    def __init__(self, bgp_agent_api, name):
        super().__init__(bgp_agent_api, name)
        self.lrp_mac = self._get_lrp_mac()
        self.patch_port_ofport = self._get_bridge_patch_port_ofport()

    def __str__(self):
        return f"BGPChassisBridge(name={self.name})"

    __repr__ = __str__

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

    def configure_flows(self):
        # TODO(jlibosva) Implement flows configuration
        pass
