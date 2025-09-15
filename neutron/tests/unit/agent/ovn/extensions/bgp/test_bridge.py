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

import netaddr

from neutron.services.bgp.agent import bridge
from neutron.tests import base


class FakeConnection:
    def __init__(self, laddr, raddr, status):
        self.laddr = laddr
        self.raddr = raddr
        self.status = status


class FakeAddr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class FindBgpConnectionsTestCase(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(
            bridge, 'get_bgp_protocol_port_number',
            return_value=179).start()
        self.m_net_connections = mock.patch(
            'psutil.net_connections',
        ).start()

        self.source_ip_list = [netaddr.IPNetwork('192.0.2.2/30')]

    def test_connecting_to_remote_peer(self):
        self.m_net_connections.return_value = [
            FakeConnection(
                laddr=FakeAddr('192.0.2.2', 12345),
                raddr=FakeAddr('192.0.2.1', 179),
                status='ESTABLISHED'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 179),
                raddr=None,
                status='LISTEN'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 12345),
                raddr=FakeAddr('192.0.2.2', 8080),
                status='ESTABLISHED'
            ),
        ]
        result = bridge.find_bgp_connections(self.source_ip_list)
        self.assertEqual([('192.0.2.2/30', '192.0.2.1')], result)

    def test_connecting_from_remote_peer(self):
        self.m_net_connections.return_value = [
            FakeConnection(
                laddr=FakeAddr('192.0.2.2', 179),
                raddr=FakeAddr('192.0.2.1', 12345),
                status='ESTABLISHED'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 179),
                raddr=None,
                status='LISTEN'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 12345),
                raddr=FakeAddr('192.0.2.2', 8080),
                status='ESTABLISHED'
            ),
        ]
        result = bridge.find_bgp_connections(self.source_ip_list)
        self.assertEqual([('192.0.2.2/30', '192.0.2.1')], result)

    def test_find_no_connections(self):
        self.m_net_connections.return_value = []
        result = bridge.find_bgp_connections(self.source_ip_list)
        self.assertEqual([], result)

    def test_find_connections_multiple(self):
        self.m_net_connections.return_value = [
            FakeConnection(
                laddr=FakeAddr('192.0.2.2', 179),
                raddr=FakeAddr('192.0.2.1', 54221),
                status='ESTABLISHED'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.2', 179),
                raddr=FakeAddr('192.0.2.1', 12345),
                status='ESTABLISHED'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 179),
                raddr=None,
                status='LISTEN'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 12345),
                raddr=FakeAddr('192.0.2.2', 8080),
                status='ESTABLISHED'
            ),
        ]
        result = bridge.find_bgp_connections(self.source_ip_list)
        self.assertEqual([('192.0.2.2/30', '192.0.2.1')], result)

    def test_find_missing_source_ip(self):
        # This is probably not possible but we are defensive
        self.m_net_connections.return_value = [
            FakeConnection(
                laddr=None,
                raddr=FakeAddr('192.0.2.1', 179),
                status='ESTABLISHED'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 179),
                raddr=None,
                status='LISTEN'
            ),
            FakeConnection(
                laddr=FakeAddr('192.0.2.1', 12345),
                raddr=FakeAddr('192.0.2.2', 8080),
                status='ESTABLISHED'
            ),
        ]
        result = bridge.find_bgp_connections(self.source_ip_list)
        self.assertEqual([], result)