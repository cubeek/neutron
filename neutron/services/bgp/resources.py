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

import abc
import enum

from neutron_lib.utils import net

from neutron.services.bgp import config
from neutron.services.bgp import constants
from neutron.services.bgp import helpers


class ResourceAction(enum.Enum):
    CREATE = 'create'
    DELETE = 'delete'
    UPDATE = 'update'


class Resource(metaclass=abc.ABCMeta):
    def __init__(self, action):
        if action and action not in ResourceAction:
            raise ValueError(f"Invalid action: {action}")
        self._action = action
        self._reconciled = False

    def reconcile(self, idl):
        with idl.transaction() as txn:
            if self._action == ResourceAction.CREATE:
                self._create(idl, txn)
            elif self._action == ResourceAction.DELETE:
                self._delete(idl, txn)
            elif self._action == ResourceAction.UPDATE:
                self._update(idl, txn)
        self._reconciled = True

    @abc.abstractmethod
    def _create(self, idl, txn):
        pass

    @property
    @abc.abstractmethod
    def name(self):
        pass

    @property
    def external_ids(self):
        return {constants.OWNER_KEY: constants.BGP_OWNER_TAG}


class Router(Resource):
    def _create(self, idl, txn):
        txn.add(idl.lr_add(self.name,  may_exist=True))
        txn.add(idl.db_set('Logical_Router', self.name,
                           ('external_ids', self.external_ids),
                           ('options', self.options)))

    @property
    def options(self):
        return {}

    def __str__(self):
        return f"Router {self.name}"

    @property
    def base_mac(self):
        return config.get_bgp_mac_base()

    def generate_mac(self):
        return next(net.random_mac_generator(self.base_mac.split(':')))


class Switch(Resource):
    def _create(self, idl, txn):
        txn.add(idl.ls_add(self.name,  may_exist=True))
        txn.add(idl.db_set('Logical_Switch', self.name,
                           ('external_ids', self.external_ids)))

    def __str__(self):
        return f"Switch {self.name}"


class MainRouter(Router):
    @property
    def name(self):
        return config.get_main_router_name()

    @property
    def options(self):
        return {
            'dynamic-routing': 'true',
            'dynamic-routing-redistribute': 'connected-as-host',
            'requested-tnl-key': config.get_bgp_router_tunnel_key(),
        }


class SwitchWithLocalnet(Switch):
    def __init__(self, action, network_name):
        super().__init__(action)
        self._network_name = network_name

    def _create(self, idl, txn):
        super()._create(idl, txn)
        localnet = SwitchPort(self._action, self)
        localnet._create(idl, txn)
        txn.add(idl.db_set(
            'Logical_Switch_Port', localnet.name,
            ('type', 'localnet'),
            ('options', {'network_name': self._network_name}),
            ('addresses', ['unknown'])))


class InterconnectSwitch(SwitchWithLocalnet):
    @property
    def name(self):
        return config.get_interconnect_switch_name()


class HaChassisGroup(Resource):
    def __init__(self, action, chassis_name):
        super().__init__(action)
        self._chassis_name = chassis_name

    def _create(self, idl, txn):
        txn.add(idl.ha_chassis_group_add(self.name, may_exist=True))
        txn.add(idl.ha_chassis_group_add_chassis(self.name, self._chassis_name, 10))

    def __str__(self):
        return f"HaChassisGroup {self.name} for chassis {self._chassis_name}"

    @property
    def name(self):
        return f'bgp-ha-group-{self._chassis_name}'


class ChassisResource(Resource):
    def __init__(self, action, chassis):
        super().__init__(action)
        self._chassis = chassis
        chassis_name = self.name.split('.')[0]

        self.router = ChassisRouter(action, chassis_name)
        self._resources = [
            HaChassisGroup(action, chassis.name),
            self.router,
        ]

        for network_name in helpers.get_chassis_provider_networks(chassis):
            ls = ChassisSwitch(action, chassis_name, network_name)
            self._resources.append(ls)
            self._resources.append(Connection(action, self.router, ls))

    def _create(self, idl, txn):
        for resource in self._resources:
            resource._create(idl, txn)

    def __str__(self):
        return (f"Chassis {self._chassis.hostname} with resources "
                f"{', '.join(f'<{res}>' for res in self._resources)}")

    @property
    def name(self):
        return self._chassis.hostname


class ChassisRouter(Router):
    def __init__(self, action, chassis_name):
        super().__init__(action)
        self._chassis_name = chassis_name

    @property
    def name(self):
        return f"bgp-router-chassis-{self._chassis_name}"

    def __str__(self):
        return f"ChassisRouter {self.name} for chassis {self._chassis_name}"

    @property
    def options(self):
        return {
            'chassis': self._chassis_name,
        }


class ChassisSwitch(SwitchWithLocalnet):
    def __init__(self, action, chassis_name, network_name):
        super().__init__(action, network_name)
        self._chassis_name = chassis_name

    @property
    def name(self):
        return f"bgp-switch-chassis-{self._chassis_name}-{self._network_name}"

    def __str__(self):
        return (f"ChassisSwitch {self.name} for chassis {self._chassis_name} "
                f"with network {self._network_name}")


class Port(Resource):
    def __init__(self, action, parent):
        super().__init__(action)
        self.parent = parent

    @abc.abstractmethod
    def connect(self, idl, txn):
        pass


class RouterPort(Port):
    def __init__(self, action, parent, mac, networks=None):
        super().__init__(action, parent)
        self._mac = mac
        self._networks = networks or []
        if not isinstance(self._networks, list):
            self._networks = [self._networks]

        self.peer = None

    @property
    def name(self):
        return f"bgp-lrp-{self.parent.name}-to-{self.peer.parent.name}"

    def __str__(self):
        return f"RouterPort {self.name}"

    def _create(self, idl, txn):
        if not self.peer:
            raise ValueError(f"RouterPort {self.name} has no peer port")
        txn.add(idl.lrp_add(self.parent.name, self.name, self._mac,
                            self._networks, external_ids=self.external_ids,
                            peer=self.peer.name,
                            may_exist=True))

    def connect(self, idl, txn):
        if not self.peer:
            raise ValueError(f"RouterPort {self.name} has no peer port")
        if isinstance(self.peer, RouterPort):
            self._connect_to_router_port(idl, txn)
        elif isinstance(self.peer, SwitchPort):
            self._connect_to_switch_port(idl, txn)
        else:
            raise TypeError(f"Invalid port type: {type(self.peer)}")

    def _connect_to_router_port(self, idl, txn):
        txn.add(idl.db_set('Logical_Router_Port', self.name,
                           peer=self.peer.name))
        txn.add(idl.db_set('Logical_Router_Port', self.peer.name,
                           peer=self.name))
        if isinstance(self.parent, ChassisRouter):
            ha_chassis_group = HaChassisGroup(self._action, self.parent.name)
            txn.add(txn.api.lrp_set_ha_chassis_group(
                self.peer.name, ha_chassis_group=ha_chassis_group.name))
        elif isinstance(self.peer.parent, ChassisRouter):
            ha_chassis_group = HaChassisGroup(
                self._action, self.peer.parent.name)
            txn.add(txn.api.lrp_set_ha_chassis_group(
                self.name, ha_chassis_group=ha_chassis_group.name))

    def _connect_to_switch_port(self, idl, txn):
        txn.add(idl.db_set('Logical_Switch_Port', self.peer.name,
                           addresses='router', type='router',
                           options={'router-port': self.name}))


class SwitchPort(Port):
    def __init__(self, action, parent):
        super().__init__(action, parent)
        self.peer = None

    @property
    def name(self):
        if self.peer:
            return f"bgp-lsp-{self.parent.name}-to-{self.peer.parent.name}"
        else:
            return f"bgp-lsp-{self.parent.name}-localnet"

    def __str__(self):
        return f"SwitchPort {self.name}"

    def _create(self, idl, txn):
        txn.add(idl.lsp_add(self.parent.name, self.name,
                            external_ids=self.external_ids, may_exist=True))

    def connect(self, idl, txn):
        if not self.peer:
            raise ValueError(f"SwitchPort {self.name} has no peer port")
        if isinstance(self.peer, SwitchPort):
            raise TypeError(f"Cannot connect a SwitchPort to a SwitchPort")
        txn.add(idl.db_set('Logical_Switch_Port', self.peer.name,
                           addresses='router', type='router',
                           options={'router-port': self.name}))


class Connection(Resource):
    def __init__(self, action, entity1, entity2, network=None,
                 chassis=None):
        super().__init__(action)
        self._entity1 = entity1
        self._entity2 = entity2
        self._network = network
        self._chassis = chassis

    @property
    def name(self):
        return f"bgp-conn-{self._entity1.name}-to-{self._entity2.name}"

    def _get_port(self, entity):
        if isinstance(entity, Router):
            return RouterPort(self._action, entity, entity.generate_mac(),
                              self._network)
        elif isinstance(entity, Switch):
            return SwitchPort(self._action, entity)
        else:
            raise TypeError(f"Invalid entity type: {type(entity)}")

    def __str__(self):
        return (f"Connection {self.name} between {self._entity1} and "
                f"{self._entity2}")

    def _create(self, idl, txn):
        port1 = self._get_port(self._entity1)
        port2 = self._get_port(self._entity2)
        port1.peer = port2
        port2.peer = port1
        port1._create(idl, txn)
        port2._create(idl, txn)
        port1.connect(idl, txn)
        port2.connect(idl, txn)