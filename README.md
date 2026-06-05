# Thread Network Topology for Home Assistant

> ## ✅ Requires a recent OTBR build (`/api/*` REST API)
>
> This integration uses the OpenThread Border Router (OTBR) **REST API**. OTBR reorganized
> that API: the old `GET /node` and `GET /diagnostics` endpoints are gone. The integration
> now targets the current REST layout under `/api/*`:
>
> - `GET /api/node` — node / leader information. Some builds return a
>   JSON:API-wrapped attributes object; others return a flat camelCase object,
>   and the integration handles both.
> - mesh diagnostics are an **asynchronous task queue**: it `POST`s tasks to `/api/actions`
>   (`updateDeviceCollectionTask` to refresh the mesh, then `getNetworkDiagnosticTask` per
>   router), polls them to completion, and reads the results from the `/api/diagnostics`
>   collection. (The `/api/devices` collection is refreshed by the task but not read, since
>   it retains stale entries for devices that have left.)
>
> **You need an OTBR build that exposes `/api/node`, `/api/actions`, and
> `/api/diagnostics`.** Older builds that only serve the legacy `/node` paths are
> not supported. If you prefer a fully native option, the **Open Home Foundation
> Matter Server** also renders a Thread mesh diagram from the Thread Network
> Diagnostics Cluster over Matter.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/felix920506/thread-topology.svg)](https://github.com/felix920506/thread-topology/releases)
[![License](https://img.shields.io/github/license/felix920506/thread-topology.svg)](LICENSE)

A Home Assistant custom integration that visualizes your Thread network topology, similar to the Zigbee network map. See your Thread Border Routers, end devices, and their connections at a glance.

## Features

- **Visual Topology Map**: See your entire Thread network structure in a markdown card
- **Device Identification**: Names routers from your Home Assistant Matter devices or by OUI, and flags the OTBR it's connected to
- **Matter Integration**: Labels Thread devices with the name you set in Home Assistant (not the model), so five identical sensors stay distinguishable
- **Link Quality Indicators**: Visual representation of connection quality (Poor/Fair/Good/Excellent)
- **WiFi vs Thread**: Separates Matter devices by transport type
- **Periodic Updates**: Every 60 seconds it discovers the network and queries per-router diagnostics to rebuild the map

## What You'll See

```text
🧵 ha-thread-bac3   (3 routers · 11 devices)

👑 IKEA ALPSTUGA  ·  Leader  ·  LQ Excellent
├─ 💤 Aqara Door Sensor
└─ 💤 Device (1C0F)

📡 Thread Router (E9DA)  ·  Router  ·  LQ Excellent  ·  🌐 connected OTBR
├─ 💤 Eve Motion
└─ 💤 Device (3C02)

📡 Thread Router (D773)  ·  Router  ·  LQ Excellent
├─ 💤 Device (F401)
└─ 💤 Device (F402)

📶 Matter over WiFi
• Smart Lock (Nuki)
• WiFi Smart Switch (SONOFF)
```

> Routers and end devices are named from a matched Home Assistant Matter device
> — using the **name you set in Home Assistant** (not the model) — matched by
> extended address. Routers fall back to vendor info, the address OUI,
> `custom_routers.yaml`, or `Thread Router (XXXX)`; unnamed children show as
> `Device (<address tail>)` when their extended address is available.

## Requirements

- Home Assistant 2024.1.0 or newer for this custom integration
- OpenThread Border Router (OTBR) REST API reachable on port 8081, exposing
  `/api/node`, `/api/actions`, and `/api/diagnostics`
- If you use the Home Assistant OTBR app (formerly add-on), use a version
  compatible with your Home Assistant installation; the current app package may
  require Home Assistant 2025.7.0 or newer
- Thread network with at least one border router

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/felix920506/thread-topology` as an **Integration**
4. Search for "Thread Network Topology" and install
5. Restart Home Assistant

### Manual Installation

1. Download the latest release from GitHub
2. Copy `custom_components/thread_topology` to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "Thread Network Topology"
4. Enter your OTBR URL (default: `http://core-openthread-border-router:8081`)
5. Click **Submit**

### Default OTBR URLs

| Setup | URL |
|-------|-----|
| Home Assistant OS with OTBR app (formerly add-on) | `http://core-openthread-border-router:8081` |
| Docker OTBR | `http://localhost:8081` or `http://<docker-host>:8081` |
| Standalone OTBR | `http://<otbr-ip>:8081` |

## Entities Created

| Entity | Description |
|--------|-------------|
| `sensor.thread_topology_map` | The topology diagram + text (state = device count) |
| `sensor.thread_network` | Network name and overview stats |
| Router sensors | One sensor per discovered router with link quality. Home Assistant generates the final entity ID from the router name and may customize or suffix it. |

## Dashboard Cards

### Graph card (force-directed mesh map)

The integration ships a **custom card** that draws the network as a graph:
routers as coloured hexagons (🔴 leader, 🔵 connected OTBR, 🟠 router), the
router-to-router links coloured and labelled by link quality, and each router's
children hanging off it. A router with no links shows as detached — making
re-parenting and dropped routers obvious at a glance.

The card's JavaScript is served and registered as a dashboard resource by the
integration **automatically** — no manual *Settings → Dashboards → Resources*
entry and no separate HACS frontend repo. After installing/updating, do a hard
browser refresh so the new resource loads.

```yaml
type: custom:thread-topology-card
entity: sensor.thread_topology_map   # optional (default)
title: Thread Network                # optional
height: 460                          # optional, px
```

### Text card (ASCII tree)

The diagram is also available as a monospace ASCII tree that Home Assistant's
**built-in Markdown card renders directly** (no custom card needed). It lists
each router with its children plus a **Mesh links** section for the
router-to-router edges:

```yaml
type: markdown
content: "{{ state_attr('sensor.thread_topology_map', 'topology_text') }}"
```

For more complete examples including stats tiles, see the [examples/lovelace-cards.yaml](examples/lovelace-cards.yaml) file.

## How It Works

1. **OTBR API**: Reads `/api/node`, triggers per-router diagnostics (by rloc16) via the `/api/actions` task queue, then reads the live `/api/diagnostics` collection
2. **Topology**: Every router present in the live diagnostics becomes a node — matching what the OTBR web UI graphs (the cached `/api/devices` list is not used, as it keeps stale entries for devices that have left). The leader is the router whose `routerId` matches `leaderData.leaderRouterId`; each router's children come from its `children` diagnostic
3. **Matter names**: Reads your Home Assistant Matter devices' Thread extended address (the "MAC address" on the device's *Matter info* panel) and matches it to the OTBR device by extended address — so **both routers and sleepy end devices** show the **name you set in Home Assistant** (the user-assigned name, not the model, so identical devices stay distinguishable). Children come from the per‑router `children` diagnostic, which includes each child's extended address

## Supported Border Routers

The integration automatically identifies:
- **The OTBR it connects to** (flagged as the "connected OTBR")
- **Amazon Eero** mesh routers
- **Apple HomePod** / HomePod Mini
- **Google Nest** Hub / WiFi
- **Samsung SmartThings** Hub / Station
- **Silicon Labs** dev boards
- **Espressif** ESP32-H2 Thread devices
- **Nordic Semiconductor** nRF52/nRF53 devices
- **Nanoleaf** controllers

### How Detection Works

Routers are identified using the **OUI prefix** (first 3 bytes) of their Thread extended address. For example, a device with extended address `AABAD11C1D3AF27F` has OUI `AA:BA:D1`.

The integration checks in this order:
1. **Custom routers** — user-defined in `custom_routers.yaml` (see below)
2. **Home Assistant Matter name** — matched by Thread extended address (the name you set in HA, not the model)
3. **Device vendor info** — the device's own `vendorName` / `vendorModel` diagnostic (e.g. `Home Assistant OpenThread Border Router`), useful for devices Home Assistant doesn't know
4. **Built-in OUI table** — ~30 known manufacturer prefixes
5. **Pattern matching** — substring patterns for specific devices
6. **Neutral fallback** — `Thread Router (XXXX)`, where `XXXX` is the last 4 hex of the extended address (assign a real name via `custom_routers.yaml`)

The border router the integration is pointed at is flagged as the **connected OTBR** in the diagram. It is whichever OTBR URL you configured for this integration.

### Custom Border Router Configuration

If your border routers aren't automatically detected, you can define them in a YAML file.

1. Copy the example file:
   ```bash
   cd custom_components/thread_topology/
   cp custom_routers.example.yaml custom_routers.yaml
   ```

2. Edit `custom_routers.yaml` with your devices:
   ```yaml
   routers:
     - address: "AA:BA:D1"
       name: "SMlight OTBR"
       manufacturer: "SMlight"
       icon: "chip"

     - address: "121BEC66640787A6"
       name: "ESP32-H2 Router"
       manufacturer: "Espressif"
       icon: "chip"
   ```

3. Restart Home Assistant (or reload the integration)

#### Finding Your Router's Extended Address

1. Go to **Settings** → **Devices & Services** → **Thread**
2. Click on your border router
3. Look for **Extended Address** (e.g., `AABAD11C1D3AF27F`)

#### Supported Address Formats

All formats are accepted and automatically normalized:

| Format | Example | Matches |
|--------|---------|---------|
| Full address | `AABAD11C1D3AF27F` | Exact device only |
| Full with colons | `AA:BA:D1:1C:1D:3A:F2:7F` | Exact device only |
| OUI prefix (3 bytes) | `AABAD1` or `AA:BA:D1` | Any device from this manufacturer |
| Partial pattern | `121BEC6664` | Any address containing this string |

> A value of exactly 6 hex chars (3 bytes) is treated as an **OUI prefix** and only
> matches the *start* of an address. For "matches anywhere" behavior, use a pattern
> **longer than 6 hex chars**.

#### Available Icons

`chip`, `router`, `appletv`, `homepod`, `nest`, `eero`, `smartthings`, `nanoleaf`, `apple`

> **Note:** the `icon` field is currently reserved and not yet rendered — the topology
> card uses role-based emoji (👑 leader, 📡 router, 💤/🔋 child). Setting an icon is
> accepted but has no visible effect today.

## Troubleshooting

### "Cannot connect to OTBR"
- Ensure the OpenThread Border Router app (formerly add-on) or your standalone
  OTBR service is running
- Check if the URL is correct (try accessing it in your browser)
- Verify network connectivity between HA and OTBR

### Devices not showing names
- The integration matches Thread devices with Matter devices in HA
- Ensure your Matter devices are properly configured in Home Assistant
- WiFi-based Matter devices won't appear in the Thread topology

### Missing end devices
- Sleepy end devices may take time to appear after joining
- Try refreshing the data by reloading the integration

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Credits

- Originally created by [Joan Josep Tortosa (@jjtortosa)](https://github.com/jjtortosa/thread-topology); maintained by [@felix920506](https://github.com/felix920506/thread-topology)
- Built for the Home Assistant community
- Inspired by the Zigbee network map functionality
- Uses the OpenThread Border Router REST API

## Support

- [GitHub Issues](https://github.com/felix920506/thread-topology/issues)
- [Home Assistant Community](https://community.home-assistant.io/)
