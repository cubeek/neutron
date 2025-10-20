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
from ovsdbapp.backend.ovs_idl import event as row_event

from neutron._i18n import _
from neutron.agent.ovn.agent import ovsdb
from neutron.common.ovn import constants as ovn_const
from neutron.services.bgp import constants


LOG = log.getLogger(__name__)


def _get_external_ids_list(row, key):
    try:
        value = row.external_ids[key]
    except KeyError:
        return []
    except AttributeError:
        return []

    if not value:
        return []

    return [item.strip() for item in value.split(',')
            if item and item.strip()]



def _get_bgp_peer_bridges(row):
    return set(_get_external_ids_list(row, constants.AGENT_BGP_PEER_BRIDGES))


def _get_ovn_bridge_mappings(row):
    """Get OVN bridge mappings as {bridge_name: 'network:bridge'} dict."""
    return {
        mapping.split(':')[1]: mapping
        for mapping in _get_external_ids_list(row, 'ovn-bridge-mappings')}


class BGPAgentEvent(row_event.RowEvent):
    """Base class for BGP agent events."""

    def __init__(self, agent_api):
        self.agent_api = agent_api
        super().__init__(self.EVENTS, self.TABLE, None)

    # TODO(jlibosva): Remove with ovsdbapp>2.13.0
    @property
    def event_name(self):
        return self.__class__.__name__

    @property
    def bgp_agent(self):
        if not hasattr(self, '_bgp_agent'):
            try:
                self._bgp_agent = self.agent_api[constants.AGENT_BGP_EXT_NAME]
            except KeyError:
                raise RuntimeError(_("BGP agent is not configured"))
        return self._bgp_agent


class LocalOVSEvent(BGPAgentEvent):
    """Base class for local OVS events."""
    TABLE = 'Open_vSwitch'

    def _get_desired_mappings(self, row, old):
        bgp_peer_bridges = set(_get_bgp_peer_bridges(row))
        current_mappings = _get_ovn_bridge_mappings(row)
        old_bgp_peer_bridges = set(_get_bgp_peer_bridges(old))

        bgp_mappings = {bridge: f"{bridge}:{bridge}"
                        for bridge in bgp_peer_bridges}

        # Keep all non-BGP mappings as-is
        non_bgp_mappings = {
            mapping
            for bridge, mapping in current_mappings.items()
            if bridge not in bgp_peer_bridges | old_bgp_peer_bridges
        }

        return sorted(list(set(bgp_mappings.values()) | non_bgp_mappings))

    def run(self, event, row, old):
        desired_mappings = self._get_desired_mappings(row, old)
        self.bgp_agent.configure_bgp_bridge_mappings(desired_mappings)


class CreateLocalOVSEvent(LocalOVSEvent):
    EVENTS = (LocalOVSEvent.ROW_CREATE,)

    def match_fn(self, event, row, old):
        if constants.AGENT_BGP_PEER_BRIDGES not in row.external_ids:
            LOG.warning("The BGP bridges are not configured")
            return False
        return True


class UpdateLocalOVSEvent(LocalOVSEvent):
    EVENTS = (LocalOVSEvent.ROW_UPDATE,)

    def match_fn(self, event, row, old):
        desired_mappings = self._get_desired_mappings(row, old)
        bm_bridges = sorted(list(_get_ovn_bridge_mappings(row).values()))

        return desired_mappings != bm_bridges


class NewBgpBridgeEvent(BGPAgentEvent):
    EVENTS = (BGPAgentEvent.ROW_CREATE, BGPAgentEvent.ROW_UPDATE,)
    TABLE = 'Bridge'

    @staticmethod
    def _get_bgp_bridges(idl):
        ovs_entries = list(idl.tables['Open_vSwitch'].rows.values())
        if len(ovs_entries) != 1:
            LOG.error(
                "Expected 1 Open_vSwitch entry, got %s", len(ovs_entries))
            return []
        bgp_bridges_text = ovs_entries[0].external_ids.get(
            constants.AGENT_BGP_PEER_BRIDGES, '')
        if bgp_bridges_text:
            return bgp_bridges_text.split(',')
        return []

    def _is_bgp_bridge(self, row):
        bgp_bridges = self._get_bgp_bridges(row._idl)
        return row.name in bgp_bridges

    def _has_nic_iface(self, row):
        for port in row.ports:
            iface = port.interfaces[0]
            if iface.type == '':
                LOG.debug("XXX found iface: %s - %s", row.name, iface.name)
                return True
        return False

    def _nic_iface_added(self, row, old):
        added_ports = set(row.ports) - set(old.ports)
        return any(port.interfaces[0].type == '' for port in added_ports)

    def match_fn(self, event, row, old):
        if not super().match_fn(event, row, old):
            return False

        if not self._is_bgp_bridge(row):
            return False

        if event == BGPAgentEvent.ROW_UPDATE:
            if (not hasattr(old, 'ports') or
                    not self._nic_iface_added(row, old)):
                return False

        return self._has_nic_iface(row)

    def run(self, event, row, old):
        LOG.debug("XXX NewBgpBridgeEvent: %s", row.name)
        bgp_bridge = self.bgp_agent.create_bgp_bridge(row.name)
        self.bgp_agent.watch_patch_port_created_event(bgp_bridge)
        bgp_bridge.discover_bgp_peer_in_thread()


class BGPBridgePatchPortCreatedEvent(BGPAgentEvent):
    EVENTS = (BGPAgentEvent.ROW_CREATE,)
    TABLE = 'Interface'
    ONETIME = True

    def __init__(self, agent_api, bgp_bridge_name):
        super().__init__(agent_api)
        self.bgp_bridge_name = bgp_bridge_name
        LOG.debug("XXX BGPBridgePatchPortCreatedEvent: %s", self.bgp_bridge_name)

    def _get_port_bridge(self, port_name):
        # We just need access to BaseOVS
        some_bridge = next(iter(self.bgp_agent.bgp_bridges.values()))
        return some_bridge.ovs_bridge.get_bridge_for_iface(port_name)

    def match_fn(self, event, row, old):
        LOG.debug("XXX type BGPBridgePatchPortCreatedEvent: %s - %s", row.name, row.type)
        if not super().match_fn(event, row, old):
            return False

        if row.type != 'patch':
            return False

        try:
            port_bridge_name = self._get_port_bridge(row.name)
        except StopIteration:
            LOG.warning("No BGP bridge found in agent.")
            return False
        LOG.debug("XXX bridge BGPBridgePatchPortCreatedEvent: %s - %s", port_bridge_name, self.bgp_bridge_name)

        return port_bridge_name == self.bgp_bridge_name

    def run(self, event, row, old):
        LOG.debug("XXX BGPBridgePatchPortCreatedEvent: %s", row.name)
        port_bridge_name = self._get_port_bridge(row.name)
        bgp_bridge = self.bgp_agent.bgp_bridges[port_bridge_name]
        bgp_bridge.patch_port_ofport = row.ofport[0]
        if bgp_bridge.lrp_mac:
            bgp_bridge.configure_flows()


class PortBindingLrpMacEvent(BGPAgentEvent):
    """Port_Binding update event - set LRP MAC."""
    TABLE = 'Port_Binding'
    EVENTS = (BGPAgentEvent.ROW_CREATE, BGPAgentEvent.ROW_UPDATE,)

    def __init__(self, agent_api):
        super().__init__(agent_api)
        self.chassis = ovsdb.get_own_chassis_name(agent_api.ovs_idl)

    def match_fn(self, event, row, old):
        if not super().match_fn(event, row, old):
            return False
        if row.type != ovn_const.PB_TYPE_L3GATEWAY:
            return False
        if row.chassis and row.chassis[0].name != self.chassis:
            return False
        if constants.BGP_CHASSIS_NETWORK_NAME not in row.external_ids:
            return False
        return True

    def run(self, event, row, old):
        LOG.debug("XXX PortBindingLrpMacEvent: %s", row.logical_port)
        lrp_mac = row.mac[0].split(' ', 1)[0]
        network_name = row.external_ids[constants.BGP_CHASSIS_NETWORK_NAME]
        try:
            bridge = self.bgp_agent.bgp_bridges[network_name]
        except KeyError:
            LOG.warning("No BGP bridge found for network %s", network_name)
            return
        bridge.lrp_mac = lrp_mac
        if bridge.patch_port_ofport:
            bridge.configure_flows()
