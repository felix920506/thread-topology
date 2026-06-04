# Thread Network Topology

Visualize your Thread mesh in Home Assistant using a built-in Markdown card. This integration reads the OpenThread Border Router REST API and shows your Thread border routers, router role, child devices, link quality, and matched Matter device names.

## Highlights

- Shows a Thread topology map without requiring a custom Lovelace card.
- Identifies the leader, routers, the connected OTBR, and end devices.
- Uses Home Assistant Matter device names when Thread extended addresses match.
- Separates Matter-over-WiFi devices from Thread devices.
- Updates the topology periodically from live OTBR diagnostics.

## Requirements

- Home Assistant 2024.1.0 or newer.
- A Thread network with at least one border router.
- An OpenThread Border Router REST API reachable from Home Assistant.
- OTBR endpoints under `/api/*`, including `/api/node`, `/api/actions`, and `/api/diagnostics`.

Older OTBR builds that only expose legacy `/node` or `/diagnostics` endpoints are not supported.

## Installation

1. Open HACS.
2. Go to Integrations.
3. Search for `Thread Network Topology`.
4. Download the integration.
5. Restart Home Assistant.
6. Go to Settings > Devices & services.
7. Add the `Thread Network Topology` integration.
8. Enter your OTBR URL.

Common OTBR URLs:

- Home Assistant OS OTBR app: `http://core-openthread-border-router:8081`
- Docker OTBR: `http://<docker-host>:8081`
- Standalone OTBR: `http://<otbr-ip>:8081`

## Dashboard Card

Add a built-in Markdown card and use the topology text attribute:

```yaml
type: markdown
content: "{{ state_attr('sensor.thread_topology_map', 'topology_text') }}"
```

## Entities

- `sensor.thread_topology_map`: topology diagram text. The state is the device count.
- `sensor.thread_network`: network name and overview statistics.
- Router sensors: one sensor per discovered Thread router with link quality data.

## Notes

The integration names routers and end devices from matching Home Assistant Matter devices when possible. Routers can also be identified by vendor data, OUI prefix, or custom router definitions.

For advanced setup, custom router naming, troubleshooting, and OTBR API details, see the full README in this repository.

## Support

- Issues: https://github.com/felix920506/thread-topology/issues
- Documentation: https://github.com/felix920506/thread-topology
