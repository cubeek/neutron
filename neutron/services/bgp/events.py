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


class BGPEntityEvent(row_event.RowEvent):
    """Base class for BGP entity events."""

    def __init__(self, reconciler, table, events):
        self.reconciler = reconciler
        super().__init__(events, table, None)
        self.event_name = self.__class__.__name__

    def match_fn(self, event, row, old=None):
        """Match BGP-owned entities by checking external_ids."""
        owner = row.external_ids.get(constants.OWNER_KEY)
        return owner == constants.BGP_OWNER_TAG

    def run(self, event, row, old=None):
        """Handle BGP entity events."""
        # For deletions, trigger full sync to check if entity needs recreation
        if event == self.ROW_DELETE:
            LOG.info("BGP entity deleted: %s. Triggering reconciliation check.",
                     getattr(row, 'name', 'unknown'))
            self.reconciler.schedule_reconciliation()
        elif event in (self.ROW_CREATE, self.ROW_UPDATE):
            # For creates/updates, verify entity integrity
            self.reconciler.verify_entity_integrity(row, old)


class BGPLogicalRouterEvent(BGPEntityEvent):
    """Event for BGP logical router changes."""

    def __init__(self, reconciler):
        super().__init__(reconciler, 'Logical_Router',
                         (self.ROW_CREATE, self.ROW_UPDATE, self.ROW_DELETE))


class BGPLogicalSwitchEvent(BGPEntityEvent):
    """Event for BGP logical switch changes."""

    def __init__(self, reconciler):
        super().__init__(reconciler, 'Logical_Switch',
                         (self.ROW_CREATE, self.ROW_UPDATE, self.ROW_DELETE))


class BGPLogicalRouterPortEvent(BGPEntityEvent):
    """Event for BGP logical router port changes."""

    def __init__(self, reconciler):
        super().__init__(reconciler, 'Logical_Router_Port',
                         (self.ROW_CREATE, self.ROW_UPDATE, self.ROW_DELETE))


class BGPLogicalSwitchPortEvent(BGPEntityEvent):
    """Event for BGP logical switch port changes."""

    def __init__(self, reconciler):
        super().__init__(reconciler, 'Logical_Switch_Port',
                         (self.ROW_CREATE, self.ROW_UPDATE, self.ROW_DELETE))


class BGPHAChassisGroupEvent(BGPEntityEvent):
    """Event for BGP HA chassis group changes."""

    def __init__(self, reconciler):
        super().__init__(reconciler, 'HA_Chassis_Group',
                         (self.ROW_CREATE, self.ROW_UPDATE, self.ROW_DELETE))


class BGPChassisEvent(row_event.RowEvent):
    """Event for BGP chassis changes."""

    def __init__(self, events):
        super().__init__(events, 'Chassis', None)
        self.event_name = self.__class__.__name__

    def match_fn(self, event, row, old=None):
        """Match BGP chassis by checking external_ids."""
        return row.external_ids.get(constants.OWNER_KEY) == constants.BGP_OWNER_TAG