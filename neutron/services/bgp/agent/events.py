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

from neutron.services.bgp import constants

LOG = log.getLogger(__name__)


def get_bgp_peer_bridges(row):
    try:
        return row.external_ids[constants.AGENT_BGP_PEER_BRIDGES].split(',')
    except KeyError:
        LOG.warning("Chassis %s does not have BGP configuration but the "
                    "BGP extension is enabled", row.name)
        return []


class BGPAgentEvent(row_event.RowEvent):
    """Base class for BGP agent events."""

    def __init__(self, agent_api):
        self.agent_api = agent_api
        super().__init__(self.EVENTS, self.TABLE, None)

    @property
    def event_name(self):
        return self.__class__.__name__

    @property
    def bgp_agent(self):
        if not hasattr(self, '_bgp_agent'):
            try:
                self._bgp_agent = self.agent_api['bgp']
            except KeyError:
                raise RuntimeError("BGP agent is not configured")
            self._bgp_agent._is_started_event.wait()
        return self._bgp_agent


class LocalOVSEvent(BGPAgentEvent):
    """Base class for local OVS events."""
    TABLE = 'Open_vSwitch'


class CreateLocalOVSEvent(LocalOVSEvent):
    EVENTS = (LocalOVSEvent.ROW_CREATE,)

    def __init__(self, agent_api):
        super().__init__(agent_api)

    def match_fn(self, event, row, old):
        if constants.AGENT_BGP_PEER_BRIDGES not in row.external_ids:
            LOG.warning("Chassis %s does not have BGP configuration but the "
                        "BGP extension is enabled", self.agent_api.chassis)
            return False
        return True

    def run(self, event, row, old):
        bgp_peer_bridge = get_bgp_peer_bridges(row)
        ovn_bridge_mappings = row.external_ids.get(
            'ovn-bridge-mappings', '').split(',')
        self.bgp_agent.configure_bgp_bridge_mappings(
            bgp_peer_bridge, ovn_bridge_mappings)


class UpdateLocalOVSEvent(LocalOVSEvent):
    EVENTS = (LocalOVSEvent.ROW_UPDATE,)

    def __init__(self, agent_api):
        super().__init__(agent_api)

    def match_fn(self, event, row, old):
        try:
            old_brs = old.external_ids.get(constants.AGENT_BGP_PEER_BRIDGES)
        except AttributeError:
            # No updates to external_ids
            return False
        current_brs = row.external_ids.get(constants.AGENT_BGP_PEER_BRIDGES)

        return old_brs != current_brs

    def run(self, event, row, old):
        bgp_peer_bridges = set(get_bgp_peer_bridges(row))
        old_bgp_peer_bridges = set(get_bgp_peer_bridges(old))
        ovn_bridge_mappings = row.external_ids.get(
            'ovn-bridge-mappings', '').split(',')

        added_bridges = bgp_peer_bridges - old_bgp_peer_bridges
        removed_bridges = old_bgp_peer_bridges - bgp_peer_bridges

        if added_bridges:
            self.bgp_agent.configure_bgp_bridge_mappings(
                list(added_bridges), ovn_bridge_mappings)

        if removed_bridges:
            self.bgp_agent.remove_bgp_bridge_mappings(
                list(removed_bridges), ovn_bridge_mappings)

        self.bgp_agent.create_bgp_bridges()
        self.bgp_agent.update_chassis_peer_connections()


class PatchPortEvent(BGPAgentEvent):
    """Base class for patch port events."""
    TABLE = 'Bridge'
    EVENTS = (BGPAgentEvent.ROW_UPDATE, BGPAgentEvent.ROW_CREATE)

    def __init__(self, agent_api):
        super().__init__(agent_api)

    def match_fn(self, event, row, old):
        if not super().match_fn(event, row, old):
            return False

        br_int = self.agent_api.ovs_idl.db_get(
            'Open_vSwitch', '.', 'external_ids').execute(check_error=True).get(
                'ovn-bridge'
            )
        if row.name == br_int:
            return False

        if event == self.ROW_CREATE:
            # Likely an agent restart
            return True

        cur_ports = set(row.ports)
        old_ports = set(old.ports)

        return cur_ports != old_ports


    def run(self, event, row, old):
        self.bgp_agent.handle_patch_ports(row.name)


class BGPChassisEvent(BGPAgentEvent):
    """Base class for BGP chassis events."""
    TABLE = 'Chassis'

    def run(self, event, row, old):
        self.bgp_agent.configure_chassis_bgp_bridges()
        self.bgp_agent.update_chassis_peer_connections()


class CreateChassisEvent(BGPChassisEvent):
    """New chassis that already has LRP MAC map configured."""
    EVENTS = (BGPChassisEvent.ROW_CREATE,)


class UpdateChassisEvent(BGPChassisEvent):
    EVENTS = (BGPChassisEvent.ROW_UPDATE,)

    def __init__(self, agent_api):
        super().__init__(agent_api)

    def match_fn(self, event, row, old):
        LOG.debug("XXX UpdateChassisEvent %s %s %s", event, row, old)
        if not super().match_fn(event, row, old):
            return False
        if not hasattr(old, 'external_ids'):
            return False
        try:
            current_lrp_mac_map = row.external_ids[
                constants.CHASSIS_BGP_LRP_MAC_MAP]
            old_lrp_mac_map = old.external_ids[constants.CHASSIS_BGP_LRP_MAC_MAP]
        except KeyError:
            return False

        return current_lrp_mac_map != old_lrp_mac_map