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
import collections
import re

from neutron_lib import context as n_context
from neutron_lib.api.definitions import provider_net
from neutron_lib.plugins import directory
import netaddr
from oslo_log import log

from neutron.services.bgp import constants

LOG = log.getLogger(__name__)

MAC_RE = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
MAC_BYTES = 6


Router = collections.namedtuple(
    'Router',
    ['mac_prefix', 'max_mac_index', 'remaining_bytes'])


class LrpMacManager:
    def __init__(self):
        self.known_routers = {}

    @classmethod
    def get_instance(cls):
        if not hasattr(cls, '_instance'):
            cls._instance = cls()
        return cls._instance

    def register_router(self, router_name, mac_prefix):
        self.known_routers[router_name] = Router(
            mac_prefix=mac_prefix,
            max_mac_index=255 ** (MAC_BYTES - len(mac_prefix.split(':'))) - 1,
            remaining_bytes=MAC_BYTES - len(mac_prefix.split(':')),
        )

    def get_mac_address(self, router_name, index):
        try:
            router = self.known_routers[router_name]
        except KeyError:
            raise RuntimeError(f"Router {router_name} not registered")

        if index > router.max_mac_index:
            raise ValueError(
                f"Index {index} is too large, maximum is "
                f"{router.max_mac_index}")

        hex_str = f"{index:0{router.remaining_bytes * 2}x}"

        hex_bytes = ':'.join(hex_str[i:i+2] for i in range(0, len(hex_str), 2))

        result = f'{router.mac_prefix}:{hex_bytes}'

        if not MAC_RE.match(result):
            raise ValueError(f"Invalid MAC address: {result}")

        return result


def get_neutron_external_network(neutron_plugin, ctx):
        external_nets = neutron_plugin.get_networks(
            ctx, filters={'router:external': [True]}
        )

        # TODO (jlibosva): Handle multiple external networks
        if len(external_nets) > 1:
            # TODO (jlibosva): Make it a custom exception
            raise Exception("Multiple external networks found, so far only one is supported")

        return external_nets[0]


def get_external_network_gw_ip():
    neutron_plugin = directory.get_plugin()
    ctx = n_context.get_admin_context()
    external_net = get_neutron_external_network(neutron_plugin, ctx)
    for subnet_id in external_net['subnets']:
        subnet = neutron_plugin.get_subnet(ctx, subnet_id)
        if subnet['ip_version'] == 4:
            gw_ip = subnet["gateway_ip"]
            prefixlen = netaddr.IPNetwork(subnet["cidr"]).prefixlen
            return f'{gw_ip}/{prefixlen}'
    raise Exception("No IPv4 gateway found for external network")


def get_provider_network_name():
    neutron_plugin = directory.get_plugin()
    ctx = n_context.get_admin_context()
    external_net = get_neutron_external_network(neutron_plugin, ctx)
    return external_net[provider_net.PHYSICAL_NETWORK]


def get_all_chassis(sb_ovn):
    chassis = sb_ovn.db_find_rows('Chassis').execute(check_error=True)
    return chassis


def get_chassis_provider_networks(chassis):
    return [
        bm.split(':')[0]
        for bm in chassis.other_config.get(
            'ovn-bridge-mappings').split(',')
        if bm.startswith('bgp-')]


def get_chassis_bgp_peer_mapping(chassis):
    for options in chassis.other_config.get('ovn-cms-options', '').split(','):
        if options.split('=')[0] == constants.OVN_BGP_PEERS_KEY:
            bgp_peers = options.split('=')[1].split(';')
            break
    else:
        raise RuntimeError(f"Chassis {chassis.name} has no BGP peer mapping")

    try:
        bgp_peer_mapping = {network_name: peer_ip
            for network_name, peer_ip in (
                bm.split(':', 1) for bm in bgp_peers
            )
        }
    except ValueError:
        raise RuntimeError(f"BGP peer mapping {bgp_peers} is not valid")

    for ip in bgp_peer_mapping.values():
        try:
            netaddr.IPAddress(ip)
        except netaddr.core.AddrFormatError:
            raise RuntimeError(f"BGP peer IP {ip} is not a valid address")

    return bgp_peer_mapping


def get_ipv4_peer_address(ip_address):
    try:
        ip_net = netaddr.IPNetwork(f"{ip_address}/30")
    except netaddr.core.AddrFormatError:
        raise ValueError(f"Invalid IPv4 address: {ip_address}")

    network = ip_net.network

    first_host = network + 1
    second_host = network + 2

    if ip_net.ip == first_host:
        return str(second_host)
    elif ip_net.ip == second_host:
        return str(first_host)
    else:
        raise RuntimeError(f"Address {ip_net.ip} is not a usable host address "
                           f"in the /30 network {network}/30")


class InternalIpManager:
    """
    This is only temperary workaround until IPv6 LLAs can be used.
    """

    IP_BASE = 169.254

    @classmethod
    def get_ip(cls, chassis_index, port_index):
        return f"{cls.IP_BASE}.{chassis_index}.{port_index}"

    @classmethod
    def get_ip_cidr(cls, chassis_index, port_index):
        return f"{cls.get_ip(chassis_index, port_index)}/30"
