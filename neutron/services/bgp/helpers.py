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

import uuid

import netaddr
from neutron_lib import context
from neutron_lib.plugins import directory
from oslo_log import log
from ovsdbapp.backend.ovs_idl import rowview

from neutron.agent.linux import ip_lib
from neutron.common.ovn import constants as ovn_const
from neutron.conf.services import bgp as bgp_config
from neutron.services.bgp import constants

LOG = log.getLogger(__name__)

BGP_LRP_UUID_NAMESPACE = uuid.UUID('9eaaac59-33fc-4f45-a450-8c220d46ad95')


def get_mac_address_from_lrp_name(lrp_name):
    mac_uuid_base = uuid.uuid5(BGP_LRP_UUID_NAMESPACE, lrp_name)

    # Let's take last 6 bytes of the UUID and convert them to bytes
    mac_bytes = bytearray(mac_uuid_base.bytes[-6:])

    # 3. Apply Bitwise Operations on the First Byte
    # Set the "Locally Administered" bit (2nd least significant bit) to 1
    # xxxxxxx1 | 00000010 = xxxxxxx1 (OR 0x02)
    mac_bytes[0] |= 0x02

    # Clear the "Multicast" bit (Least significant bit) to 0
    # xxxxxxx1 & 11111110 = xxxxxxx0 (AND 0xfe)
    mac_bytes[0] &= 0xfe

    # 4. Format into standard MAC string (XX:XX:XX:XX:XX:XX)
    mac_address = ':'.join(f'{b:02x}' for b in mac_bytes)

    return mac_address


def get_chassis_bgp_bridges(chassis):
    try:
        bgp_bridges = chassis.external_ids[
            constants.CHASSIS_BGP_BRIDGES_EXT_ID_KEY]
    except KeyError:
        LOG.warning("Chassis %s has no BGP bridges set", chassis.name)
        return []

    return [bridge for br in bgp_bridges.split(',') if (bridge := br.strip())]


# Naming helper functions
def get_lrp_name(from_name, to_name):
    return f'bgp-lrp-{from_name}-to-{to_name}'


def get_lsp_name(from_name, to_name):
    return f'bgp-lsp-{from_name}-to-{to_name}'


def get_lsp_localnet_name(switch_name):
    return f'bgp-lsp-{switch_name}-localnet'


def get_hcg_name(chassis_name):
    return f'bgp-hcg-{chassis_name}'


def get_chassis_router_name(chassis_name):
    return f'bgp-lr-{chassis_name}'


def get_chassis_peer_switch_name(chassis_name, network_name):
    return f'bgp-ls-{chassis_name}-{network_name}'


def get_provider_interconnect_switch_name(provider_switch_name):
    return f'bgp-ls-interconnect-{provider_switch_name}'


def get_subnet(subnet_id):
    return directory.get_plugin().get_subnet(
        context.get_admin_context(),
        subnet_id)


def get_network_prefixlen(net):
    return netaddr.IPNetwork(net).prefixlen


def get_gw_ip(subnet):
    return (f"{subnet['gateway_ip']}/"
            f"{get_network_prefixlen(subnet['cidr'])}")


def get_neutron_network_id(switch_name):
    return switch_name.split('neutron-', 1)[1]


# Router filtering helpers
def _get_lrps_by_external_id(router, external_id):
    return [lrp for lrp in router.ports
            if hasattr(lrp, 'external_ids') and
            external_id in lrp.external_ids]


def lrps_to_chassis_routers(router):
    return _get_lrps_by_external_id(router, constants.BGP_LRP_TO_CHASSIS)


def lrps_to_neutron(router):
    return _get_lrps_by_external_id(router, constants.BGP_LRP_TO_NEUTRON)


# IDL-dependent helpers
def get_lrp_peer_ip(idl, lrp):
    peer_lrp = idl.lookup('Logical_Router_Port', lrp.peer[0])
    try:
        return peer_lrp.networks[0].split('/')[0]
    except IndexError:
        return ip_lib.get_ipv6_lladdr(peer_lrp.mac)


def get_main_router(idl):
    return idl.lookup('Logical_Router', bgp_config.get_main_router_name())


def get_all_provider_switches(idl):
    return [
        rowview.RowView(s)
        for s in idl.tables['Logical_Switch'].rows.values()
        if s.external_ids.get(
            ovn_const.OVN_NETTYPE_EXT_ID_KEY) in
            constants.PROVIDER_NETWORK_TYPES]


def get_gw_ips(switch):
    provider_subnets = directory.get_plugin().get_subnets(
        context.get_admin_context(),
        filters={
            'network_id': get_neutron_network_id(switch.name)
        },
    )
    if provider_subnets:
        return [
            get_gw_ip(subnet)
            for subnet in provider_subnets]
    else:
        LOG.warning(f"No provider subnets found for switch "
                    f"{switch.name}, skipping")
        return []
