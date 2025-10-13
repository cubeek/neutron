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
        self.bgp_agent.load_bgp_bridges()
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
    EVENTS = (BGPAgentEvent.ROW_CREATE,)
    TABLE = 'Bridge'

    def match_fn(self, event, row, old):
        if not super().match_fn(event, row, old):
            return False
        bgp_bridges = self._get_bgp_bridges(row._idl)
        return row.name in self.bgp_agent.bgp_bridges

    @staticmethod
    def _get_bgp_bridges(idl):
        ovs_entries = list(idl.tables['Open_vSwitch'].rows.values())
        if len(ovs_entries) != 1:
            LOG.error(
                "Expected 1 Open_vSwitch entry, got %s", len(ovs_entries))
            return []
        bgp_bridges_text = ovs_entries[0].external_ids.get(
            'bgp-peer-bridges', '')
        if bgp_bridges_text:
            return bgp_bridges_text.split(',')
        return []

    def run(self, event, row, old):
        bgp_bridge = self.bgp_agent.bgp_bridges[row.name]
        bgp_bridge.configure_flows()
        self.bgp_agent.update_chassis_peer_connections()


class BGPChassisEvent(BGPAgentEvent):
    """Base class for BGP chassis events."""
    TABLE = 'Chassis'

    def match_fn(self, event, row, old):
        if not super().match_fn(event, row, old):
            return False
        return row.name == self.agent_api.chassis

    def run(self, event, row, old):
        self.bgp_agent.configure_all_chassis_bgp_bridges()
        self.bgp_agent.update_chassis_peer_connections()


class CreateChassisEvent(BGPChassisEvent):
    """New chassis that already has LRP MAC map configured."""
    EVENTS = (BGPChassisEvent.ROW_CREATE,)


class PortBindingLrpMacEvent(BGPAgentEvent):
    """Port_Binding update event - set LRP MAC."""
    TABLE = 'Port_Binding'
    EVENTS = (BGPChassisEvent.ROW_CREATE, BGPChassisEvent.ROW_UPDATE)

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
        lrp_mac = row.mac[0].split(' ', 1)[0]
        self.bgp_agent.configure_chassis_bgp_bridge(
            row.external_ids[constants.BGP_CHASSIS_NETWORK_NAME],
            lrp_mac)
