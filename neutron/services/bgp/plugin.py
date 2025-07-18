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

from neutron_lib.plugins import directory
from neutron_lib.services import base as service_base
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from oslo_config import cfg
from oslo_log import log

from neutron.plugins.ml2 import plugin as ml2_plugin
from neutron.services.bgp import config
from neutron.services.bgp import reconciler

LOG = log.getLogger(__name__)


@registry.has_registry_receivers
class BGPServicePlugin(service_base.ServicePluginBase):
    """BGP Service Plugin"""

    supported_extension_aliases = []

    def __init__(self):
        LOG.info("Starting BGP Service Plugin")
        super().__init__()
        self._nb_ovn = None
        self._sb_ovn = None
        self._ovn_driver = None
        self._reconciler = None
        self._post_fork_initialized = False
        cfg.CONF.register_opts(config.bgp_opts, group='bgp')

        self._validate_plugin_dependencies()

        # Subscribe to post-fork initialization event
        registry.subscribe(self._post_fork_initialize,
                          resources.PROCESS,
                          events.AFTER_INIT)

    def get_plugin_description(self):
        return "BGP service plugin for OVN"

    @classmethod
    def get_plugin_type(cls):
        return "bgp-service"

    def _post_fork_initialize(self, resource, event, trigger, payload=None):
        """Initialize BGP service after OVN driver post-fork initialization."""
        if self._post_fork_initialized:
            return

        LOG.info("BGP service plugin post-fork initialization starting")

        # Wait for OVN driver to be fully initialized
        if hasattr(self._ovn_driver, '_post_fork_event'):
            self._ovn_driver._post_fork_event.wait()

        # Now we can safely create the reconciler with active IDL connections
        self._reconciler = reconciler.BGPTopologyReconciler(
            self.nb_ovn,
            self.sb_ovn,
            cfg.CONF.bgp)

        # Register BGP events now that IDL is connected
#        self._register_bgp_events()

        # Perform initial full sync with distributed lock
        # Only one worker across the cluster will run this
        self._perform_initial_full_sync()

        self._post_fork_initialized = True
        LOG.info("BGP service plugin post-fork initialization completed")

    @property
    def nb_ovn(self):
        if not self._ovn_driver:
            raise RuntimeError("OVN driver not available")
        return self._ovn_driver.nb_ovn

    @property
    def sb_ovn(self):
        if not self._ovn_driver:
            raise RuntimeError("OVN driver not available")
        return self._ovn_driver.sb_ovn

    def _perform_initial_full_sync(self):
        """Perform initial BGP topology sync with distributed lock protection.
        """

        try:
            self._reconciler.full_sync()
            LOG.info("BGP initial full sync completed successfully")
        except Exception as e:
            LOG.exception("Error during BGP initial full sync: %s", e)

    def _validate_plugin_dependencies(self):
        """Validate that required plugin dependencies are available.

        Raises:
            RuntimeError: If required plugins or drivers are not available.
        """
        try:
            core_plugin = directory.get_plugin()
            try:
                if not isinstance(core_plugin, ml2_plugin.Ml2Plugin):
                    plugin_type = type(core_plugin).__name__
                    raise RuntimeError(
                        f"BGP service plugin requires ML2 plugin, but found "
                        f"'{plugin_type}' instead. Please set 'core_plugin = "
                        f"ml2' in neutron.conf.")
            except AttributeError:
                raise RuntimeError(
                    "No core plugin found. BGP service plugin requires ML2 "
                    "plugin with OVN mechanism driver to be enabled.")

            ovn_driver_ext = (core_plugin.mechanism_manager.
                              mech_drivers.get('ovn'))
            if not ovn_driver_ext:
                available_drivers = list(
                    core_plugin.mechanism_manager.mech_drivers.keys())
                raise RuntimeError(
                    f"OVN mechanism driver not found. BGP service plugin "
                    f"requires OVN mechanism driver to be enabled. Available "
                    f"drivers: {available_drivers}. Please add 'ovn' to the "
                    f"mechanism_drivers configuration option.")

            self._ovn_driver = ovn_driver_ext.obj

            LOG.info("Plugin dependencies validated successfully")

        except Exception as e:
            LOG.error("Failed to validate plugin dependencies: %s", e)
            raise

#    def _register_bgp_events(self):
#        """Register BGP entity events with OVN IDL notify handler."""
#        try:
#            if not self._reconciler:
#                LOG.warning("Cannot register BGP events: reconciler not "
#                            "initialized")
#                return
#
#            if not self._post_fork_initialized:
#                LOG.warning("Cannot register BGP events: post-fork "
#                            "initialization not complete")
#                return
#
#            # Get BGP events from reconciler
#            bgp_events = self._reconciler.get_events()
#
#            if not bgp_events:
#                LOG.warning("No BGP events to register")
#                return
#
#            # Register events with both NB and SB IDL notify handlers
#            # BGP entities are primarily in NB database
#            nb_notify_handler = self._ovn_driver.nb_ovn.idl.notify_handler
#            if hasattr(nb_notify_handler, 'watch_events'):
#                nb_notify_handler.watch_events(bgp_events)
#                LOG.info("Registered %d BGP events with OVN NB IDL",
#                         len(bgp_events))
#            else:
#                LOG.warning("OVN NB IDL notify handler does not support "
#                            "watch_events")
#
#        except Exception as e:
#            LOG.error("Failed to register BGP events: %s", e)
#            # Don't re-raise as this is not critical for basic plugin
#            # functionality