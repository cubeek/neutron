# BGP Reconciler Service Plugin

## Overview

The BGP Reconciler Service Plugin is a Neutron service plugin that maintains BGP topology entities in the OVN northbound database. It ensures that the BGP topology created by the `create-bgp.sh` script remains consistent and automatically recreates any missing entities.

## Features

- **Automatic BGP Topology Maintenance**: Continuously monitors and maintains BGP routers, switches, and connectivity
- **Per-Chassis Management**: Dynamically creates and manages topology for each compute chassis
- **Periodic Reconciliation**: Runs periodic checks to detect and recreate missing entities
- **OVN Integration**: Works directly with OVN northbound and southbound databases
- **Configurable**: Fully configurable through Neutron configuration files

## BGP Topology Components

The plugin maintains the following entities:

1. **Main BGP Router** (`bgp-main-router`) - Distributed router with BGP enabled
2. **Interconnect Switch** (`ls-interconnect`) - Connects BGP router to external networks
3. **Per-Chassis Routers** - Individual routers for each compute chassis
4. **Per-Chassis Switches** - Local switches for each chassis (eth2, eth3 interfaces)
5. **HA Chassis Groups** - High availability groups for chassis
6. **Logical Router Ports** - Connections between routers and switches
7. **Routes and Policies** - BGP routing configuration
8. **Localnet Ports** - Bridge to physical networks

## Configuration

Add the following to your `neutron.conf`:

```ini
[DEFAULT]
service_plugins = bgp-reconciler

[bgp_reconciler]
# Time in seconds between reconciliation checks (default: 60)
reconcile_interval = 60

# Name of the interconnect switch (default: ls-interconnect)
interconnect_switch_name = ls-interconnect

# Name of the main BGP router (default: bgp-main-router)
bgp_router_name = bgp-main-router

# Public subnet CIDR (default: 192.168.111.0/24)
public_subnet = 192.168.111.0/24

# Gateway IP address (default: 192.168.111.30)
gateway_ip = 192.168.111.30

# Physical network name for localnet ports (default: datacentre)
physical_network = datacentre
```

## Installation

1. Ensure the BGP reconciler plugin files are in the neutron source tree
2. Update your neutron configuration to include the service plugin
3. Restart the neutron-server service

## Usage

Once configured and started, the plugin will:

1. Automatically detect compute chassis from the OVN southbound database
2. Create the main BGP router and interconnect switch if they don't exist
3. Create per-chassis topology for each detected compute node
4. Periodically check for missing entities and recreate them

## Monitoring

The plugin logs its activities to the neutron server log. Key log messages include:

- `"Starting BGP Reconciler Plugin"` - Plugin initialization
- `"BGP topology reconciliation completed successfully"` - Successful reconciliation cycle
- `"Creating chassis topology for {chassis_name}"` - New chassis detected
- `"Error during BGP topology reconciliation"` - Reconciliation errors

## Testing

### Unit Tests

Run unit tests with:
```bash
python -m pytest neutron/tests/unit/services/bgp_reconciler/
```

### Functional Tests

Run functional tests with:
```bash
python -m pytest neutron/tests/functional/services/bgp_reconciler/
```

## Troubleshooting

### Common Issues

1. **Plugin fails to start**: Check OVN database connectivity
2. **Topology not created**: Verify chassis are properly registered in OVN southbound
3. **Reconciliation errors**: Check neutron server logs for detailed error messages

### Debug Configuration

Enable debug logging for the plugin:

```ini
[DEFAULT]
debug = True

[logger_neutron.services.bgp_reconciler]
level = DEBUG
handlers = stderr
qualname = neutron.services.bgp_reconciler
```

## Architecture

The plugin consists of two main components:

1. **BGPReconcilerPlugin**: Service plugin that handles periodic tasks and OVN connections
2. **BGPTopologyReconciler**: Core logic for detecting and creating BGP topology entities

The reconciler follows this workflow:

1. Query OVN southbound for active compute chassis
2. Ensure interconnect switch exists
3. Ensure main BGP router exists with proper configuration
4. For each chassis:
   - Create chassis-specific router
   - Create HA chassis group
   - Create and connect chassis switches
   - Configure routing policies and routes

## Related Files

- `neutron/services/bgp_reconciler/plugin.py` - Main service plugin
- `neutron/services/bgp_reconciler/reconciler.py` - Reconciliation logic
- `tools/create-bgp.sh` - Original script that creates the BGP topology
- `neutron/tests/unit/services/bgp_reconciler/` - Unit tests
- `neutron/tests/functional/services/bgp_reconciler/` - Functional tests