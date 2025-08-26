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


def get_chassis_bgp_peer_mapping(chassis):
    bgp_peer_mapping = {}
    try:
        connections = chassis.external_ids[
            constants.CHASSIS_PEER_CONNECTIONS].split(',')
    except KeyError:
        LOG.warning("Chassis %s has no BGP connection", chassis.name)
        return bgp_peer_mapping

    for connection in connections:
        network_name, source_ip, peer_ip = connection.split(':')

        try:
            source_ip = netaddr.IPAddress(source_ip)
            peer_ip = netaddr.IPAddress(peer_ip)
        except netaddr.core.AddrFormatError:
            LOG.warning("Invalid BGP peer mapping %s for chassis %s",
                        connection, chassis.name)
            continue
        bgp_peer_mapping[network_name] = (source_ip, peer_ip)

    return bgp_peer_mapping


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
