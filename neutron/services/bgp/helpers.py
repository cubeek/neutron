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


def get_neutron_external_network(neutron_plugin, ctx):
        external_nets = neutron_plugin.get_networks(
            ctx, filters={'router:external': [True]}
        )

        # TODO (jlibosva): Handle multiple external networks
        if len(external_nets) > 1:
            # TODO (jlibosva): Make it a custom exception
            raise Exception("Multiple external networks found, so far only one is supported")

        return external_nets[0]


def get_all_chassis(sb_ovn):
    chassis = sb_ovn.db_find_rows('Chassis').execute(check_error=True)
    return chassis


def get_chassis_provider_networks(chassis):
    return [
        bm.split(':')[0]
        for bm in chassis.other_config.get(
            'ovn-bridge-mappings').split(',')
        if bm.startswith('bgp-')]