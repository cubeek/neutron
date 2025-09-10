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

from unittest import mock

from neutron.services.bgp import constants
from neutron.services.bgp import helpers
from neutron.tests import base


class LrpMacManagerTestCase(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        self.manager = helpers.LrpMacManager.get_instance()
        self.manager.known_routers.clear()

    def test_singleton_instance(self):
        instance2 = helpers.LrpMacManager.get_instance()
        self.assertIs(self.manager, instance2)

    def test_register_router_valid_prefix(self):
        router_name = "test-router"
        mac_prefix = "aa:bb:cc"

        self.manager.register_router(router_name, mac_prefix)

        self.assertIn(router_name, self.manager.known_routers)
        router = self.manager.known_routers[router_name]
        self.assertEqual(router.mac_prefix, mac_prefix)
        # Should have 3 remaining bytes (6 total - 3 prefix)
        self.assertEqual(router.remaining_bytes, 3)
        # Max index for 3 bytes is 255^3 - 1
        self.assertEqual(router.max_mac_index, 255 ** 3 - 1)

    def test_register_router_different_prefix_lengths(self):
        test_cases = [
            ("aa", 5, 255 ** 5 - 1),
            ("aa:bb", 4, 255 ** 4 - 1),
            ("aa:bb:cc", 3, 255 ** 3 - 1),
            ("aa:bb:cc:dd", 2, 255 ** 2 - 1),
            ("aa:bb:cc:dd:ee", 1, 255 ** 1 - 1),
        ]

        for prefix, expected_remaining, expected_max in test_cases:
            router_name = f"router-{prefix.replace(':', '')}"
            self.manager.register_router(router_name, prefix)
            router = self.manager.known_routers[router_name]
            self.assertEqual(router.remaining_bytes, expected_remaining)
            self.assertEqual(router.max_mac_index, expected_max)

    def test_get_mac_address_valid_index(self):
        router_name = "test-router"
        mac_prefix = "aa:bb:cc"
        self.manager.register_router(router_name, mac_prefix)

        mac = self.manager.get_mac_address(router_name, 1)
        self.assertEqual(mac, "aa:bb:cc:00:00:01")

        mac = self.manager.get_mac_address(router_name, 256)
        self.assertEqual(mac, "aa:bb:cc:00:01:00")

        mac = self.manager.get_mac_address(router_name, 65536)
        self.assertEqual(mac, "aa:bb:cc:01:00:00")

    def test_get_mac_address_unregistered_router(self):
        self.assertRaises(
            RuntimeError,
            self.manager.get_mac_address, "nonexistent-router", 1)

    def test_get_mac_address_index_too_large(self):
        router_name = "test-router"
        mac_prefix = "aa:bb:cc:dd:ee"  # Only 1 remaining byte
        self.manager.register_router(router_name, mac_prefix)

        # Max index for 1 byte is 255
        self.assertRaises(
            ValueError, self.manager.get_mac_address, router_name, 256)

    def test_get_mac_address_zero_index(self):
        router_name = "test-router"
        mac_prefix = "aa:bb:cc"
        self.manager.register_router(router_name, mac_prefix)

        mac = self.manager.get_mac_address(router_name, 0)
        self.assertEqual(mac, "aa:bb:cc:00:00:00")

    def test_get_mac_address_formatting(self):
        router_name = "test-router"
        mac_prefix = "aa:bb"
        self.manager.register_router(router_name, mac_prefix)

        # Test various indices to ensure proper formatting
        test_cases = [
            (1, "aa:bb:00:00:00:01"),
            (255, "aa:bb:00:00:00:ff"),
            (256, "aa:bb:00:00:01:00"),
            (65535, "aa:bb:00:00:ff:ff"),
            (65536, "aa:bb:00:01:00:00"),
        ]

        for index, expected in test_cases:
            mac = self.manager.get_mac_address(router_name, index)
            self.assertEqual(mac, expected)

    def test_mac_invalid_index(self):
        router_name = "test-router"
        mac_prefix = "aa:bb:cc"
        self.manager.register_router(router_name, mac_prefix)

        self.assertRaises(
            ValueError, self.manager.get_mac_address, router_name, -1)


class GetChassisBgpPeerMappingTestCase(base.BaseTestCase):
    class FakeChassis:
        def __init__(self, name, external_ids=None):
            self.name = name
            self.external_ids = external_ids or {}

    def setUp(self):
        super().setUp()
        self.chassis = self.FakeChassis('test-chassis')

    def test_get_chassis_bgp_peer_mapping_valid_connections(self):
        mapping = 'net1;192.168.1.1/30;192.168.1.2,net2;10.0.0.1/30;10.0.0.2'
        self.chassis.external_ids = {
            constants.CHASSIS_PEER_CONNECTIONS: mapping
        }

        result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        expected = {
            'net1': ('192.168.1.1/30', '192.168.1.2'),
            'net2': ('10.0.0.1/30', '10.0.0.2')
        }
        self.assertEqual(expected, result)

    def test_get_chassis_bgp_peer_mapping_no_connections(self):
        self.chassis.external_ids = {}

        with mock.patch.object(helpers, 'LOG'):
            result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        self.assertEqual({}, result)

    def test_get_chassis_bgp_peer_mapping_invalid_ip_format(self):
        mapping = 'net1;invalid-ip;192.168.1.2,net2;10.0.0.1/30;10.0.0.2'
        self.chassis.external_ids = {
            constants.CHASSIS_PEER_CONNECTIONS: mapping
        }

        with mock.patch.object(helpers, 'LOG'):
            result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        # Should only return valid connections
        expected = {
            'net2': ('10.0.0.1/30', '10.0.0.2')
        }
        self.assertEqual(expected, result)

    def test_get_chassis_bgp_peer_mapping_malformed_connection_string(self):
        mapping = 'net1;192.168.1.1/30,net2;10.0.0.1/30;10.0.0.2'
        self.chassis.external_ids = {
            constants.CHASSIS_PEER_CONNECTIONS: mapping
        }

        expected = {
            'net2': ('10.0.0.1/30', '10.0.0.2')
        }

        with mock.patch.object(helpers, 'LOG'):
            result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        self.assertEqual(expected, result)

    def test_get_chassis_bgp_peer_mapping_empty_connection_string(self):
        self.chassis.external_ids = {constants.CHASSIS_PEER_CONNECTIONS: ''}

        result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        self.assertEqual({}, result)

    def test_get_chassis_bgp_peer_mapping_mixed_ip_versions(self):
        mapping = ('net1;192.168.1.1/30;192.168.1.2,'
                   'net2;2001:db8::1/64;2001:db8::2')
        self.chassis.external_ids = {
            constants.CHASSIS_PEER_CONNECTIONS: mapping
        }

        result = helpers.get_chassis_bgp_peer_mapping(self.chassis)

        expected = {
            'net1': ('192.168.1.1/30', '192.168.1.2'),
            'net2': ('2001:db8::1/64', '2001:db8::2')
        }
        self.assertEqual(expected, result)


class InternalIpManagerTestCase(base.BaseTestCase):
    def test_get_ip_basic(self):
        ip = helpers.InternalIpManager.get_ip(1, 2)
        self.assertEqual(ip, "169.254.1.2")

    def test_get_ip_different_indices(self):
        test_cases = [
            (0, 0, "169.254.0.0"),
            (1, 1, "169.254.1.1"),
            (255, 255, "169.254.255.255"),
            (10, 20, "169.254.10.20"),
        ]

        for chassis_idx, port_idx, expected in test_cases:
            ip = helpers.InternalIpManager.get_ip(chassis_idx, port_idx)
            self.assertEqual(ip, expected)

    def test_get_ip_invalid_indices(self):
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip, -1, 0)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip, 0, -1)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip, 256, 0)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip, 0, 256)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip, 256, 256)

    def test_get_ip_cidr_basic(self):
        ip_cidr = helpers.InternalIpManager.get_ip_cidr(1, 2)
        self.assertEqual(ip_cidr, "169.254.1.2/30")

    def test_get_ip_cidr_different_indices(self):
        test_cases = [
            (0, 0, "169.254.0.0/30"),
            (1, 1, "169.254.1.1/30"),
            (255, 255, "169.254.255.255/30"),
        ]

        for chassis_idx, port_idx, expected in test_cases:
            ip_cidr = helpers.InternalIpManager.get_ip_cidr(
                chassis_idx, port_idx)
            self.assertEqual(ip_cidr, expected)

    def test_ip_base_constant(self):
        self.assertEqual(helpers.InternalIpManager.IP_BASE, 169.254)

    def test_get_ip_large_indices(self):
        ip = helpers.InternalIpManager.get_ip(255, 255)
        self.assertEqual(ip, "169.254.255.255")

        ip_cidr = helpers.InternalIpManager.get_ip_cidr(255, 255)
        self.assertEqual(ip_cidr, "169.254.255.255/30")

    def test_get_ip_cidr_invalid_indices(self):
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip_cidr, -1, 0)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip_cidr, 0, -1)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip_cidr, 256, 0)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip_cidr, 0, 256)
        self.assertRaises(
            ValueError, helpers.InternalIpManager.get_ip_cidr, 256, 256)
