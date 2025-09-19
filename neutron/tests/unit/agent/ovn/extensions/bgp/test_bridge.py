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

import socket
from unittest import mock

import netaddr

from neutron.agent.ovn.extensions.bgp import bridge
from neutron.tests import base


class FakeAddr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class FakeConnection:
    def __init__(self, laddr, raddr, status):
        self.laddr = laddr
        self.raddr = raddr
        self.status = status


class GetBgpProtocolPortNumberTestCase(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        self.m_getservbyname = mock.patch.object(
            socket, 'getservbyname').start()

    def test_get_bgp_protocol_port_number(self):
        self.m_getservbyname.return_value = 179
        self.assertEqual(179, bridge.get_bgp_protocol_port_number())

    def test_get_bgp_protocol_port_number_not_found(self):
        self.m_getservbyname.side_effect = socket.error
        self.assertEqual(179, bridge.get_bgp_protocol_port_number())


class FindBgpConnectionsTestCase(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(
            bridge, 'get_bgp_protocol_port_number', return_value=179).start()
        self.m_net_connections = mock.patch('psutil.net_connections').start()
        self.source_ip_list = [
            netaddr.IPNetwork('192.168.1.1/30'),
            netaddr.IPNetwork('10.0.0.1/30'),
            netaddr.IPNetwork('172.16.1.1/32'),
        ]

    def _test_connections(self, net_connections, expected_connections):
        self.m_net_connections.return_value = net_connections
        bgp_connections = bridge.find_bgp_connections(self.source_ip_list)
        self.assertCountEqual(expected_connections, bgp_connections)

    def test_find_bgp_connections_connection_to_remote(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
            # BGP connection to remote
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),
        ]
        expected_connections = [
            ('192.168.1.1/30', '192.168.1.2'),
        ]
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_connection_to_local(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
            # BGP connection to local
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='ESTABLISHED'),
        ]
        expected_connections = [
            ('192.168.1.1/30', '192.168.1.2'),
        ]
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_connection_to_local_and_remote(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
            # BGP connection to local
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='ESTABLISHED'),
            # BGP connection to remote
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),
        ]
        # There are two BGP connections, but only one connection should be
        # returned
        expected_connections = [
            ('192.168.1.1/30', '192.168.1.2'),
        ]
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_no_connections(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
        ]
        expected_connections = []
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_no_established_connections(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
            # BGP connection to local
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='CLOSE_WAIT'),
            # BGP connection to remote
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='CLOSE_WAIT'),
        ]
        expected_connections = []
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_unmonitored_source_ip(self):
        net_connections = [
            # laddr is not in source_ip_list
            FakeConnection(
                laddr=FakeAddr('192.168.2.1', 12345),
                raddr=FakeAddr('192.168.2.2', 179),
                status='ESTABLISHED'),
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
        ]
        expected_connections = []
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_multiple_source_ips(self):
        net_connections = [
            # Some connection
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 80),
                status='ESTABLISHED'),
            # BGP connection to local
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),
            FakeConnection(
                laddr=FakeAddr('10.0.0.1', 179),
                raddr=FakeAddr('10.0.0.2', 12345),
                status='ESTABLISHED'),
        ]
        expected_connections = [
            ('192.168.1.1/30', '192.168.1.2'),
            ('10.0.0.1/30', '10.0.0.2'),
        ]
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_various_non_established_states(self):
        net_connections = [
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='LISTEN'),
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='SYN_SENT'),
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='SYN_RECV'),
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='FIN_WAIT1'),
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 179),
                raddr=FakeAddr('192.168.1.2', 12345),
                status='TIME_WAIT'),
        ]
        expected_connections = []
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_empty_source_ip_list(self):
        self.source_ip_list = []
        net_connections = [
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),
        ]
        expected_connections = []
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_ipv6(self):
        self.source_ip_list = [
            netaddr.IPNetwork('2001:db8::1/64'),
        ]
        net_connections = [
            FakeConnection(
                laddr=FakeAddr('2001:db8::1', 12345),
                raddr=FakeAddr('2001:db8::2', 179),
                status='ESTABLISHED'),
        ]
        expected_connections = [
            ('2001:db8::1/64', '2001:db8::2'),
        ]
        self._test_connections(net_connections, expected_connections)

    def test_find_bgp_connections_duplicate_connections(self):
        net_connections = [
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),
            FakeConnection(
                laddr=FakeAddr('192.168.1.1', 12345),
                raddr=FakeAddr('192.168.1.2', 179),
                status='ESTABLISHED'),  # Duplicate
        ]
        expected_connections = [
            ('192.168.1.1/30', '192.168.1.2'),
        ]
        self._test_connections(net_connections, expected_connections)
