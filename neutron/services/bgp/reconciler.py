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

from neutron.services.bgp import commands
from neutron.services.bgp import helpers

LOG = log.getLogger(__name__)


class BGPTopologyReconciler:
    def __init__(self, nb_ovn, sb_ovn):
        self.nb_ovn = nb_ovn
        self.sb_ovn = sb_ovn

    def full_sync(self):
        if not self.nb_ovn.ovsdb_connection.idl.is_lock_contended:
            LOG.info("Starting BGP topology full synchronization")

            provider_network_name = helpers.get_provider_network_name()

            chassis = commands.IndexAllChassis(
                self.sb_ovn).execute(check_error=True)
            commands.FullSyncBGPTopologyCommand(
                self.nb_ovn,
                chassis,
            ).execute(check_error=True)
            if provider_network_name:
                external_network_gw_ip = helpers.get_external_network_gw_ip()
                commands.ProvisionProviderNetworkCommand(
                    self.nb_ovn,
                    provider_network_name,
                    external_network_gw_ip,
                ).execute(check_error=True)
        else:
            LOG.info("BGP topology full synchronization already in progress")

    # TODO(jlibosva): Add a events listening for BGP resources
    # reconciling on demand