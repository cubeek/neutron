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

import enum

from neutron_lib import constants as n_lib_const

BGP_ROUTER_REDISTRIBUTE = 'connected-as-host,nat'

LRP_OPTIONS_DYNAMIC_ROUTING_MAINTAIN_VRF = 'dynamic-routing-maintain-vrf'
LRP_NETWORK_NAME_EXT_ID_KEY = 'neutron-bgp-network-name'

LR_OPTIONS_DYNAMIC_ROUTING = 'dynamic-routing'
LR_OPTIONS_DYNAMIC_ROUTING_REDISTRIBUTE = 'dynamic-routing-redistribute'
LR_OPTIONS_DYNAMIC_ROUTING_VRF_ID = 'dynamic-routing-vrf-id'

HA_CHASSIS_GROUP_PRIORITY = 10

CHASSIS_BGP_BRIDGES_EXT_ID_KEY = 'neutron-bgp-bridges'

AGENT_BGP_PEER_BRIDGES = 'neutron-bgp-peer-bridges'
AGENT_BGP_EXT_NAME = 'ovn-bgp'

BGP_BRIDGE_NIC_TYPES = ('', 'system')

BGP_PORT_NUMBER = 179

BGP_LRP_TO_CHASSIS = 'neutron-bgp-lrp-to-chassis-router'
BGP_LRP_TO_NEUTRON = 'neutron-bgp-lrp-to-neutron'

PROVIDER_NETWORK_TYPES = [n_lib_const.TYPE_FLAT, n_lib_const.TYPE_VLAN]

RELATED_RESOURCE_TAG = 'neutron-bgp-related-resource-uuid'


class BGPReconcilerResource(enum.Enum):
    CHASSIS_BGP_BRIDGES = 'bgp-bridges'
    PROVIDER_SWITCH = 'provider-switch'
    GATEWAY_IP_ROUTE = 'gateway-ip-route'

    def __str__(self):
        return self.value
