"""Data coordinator for Thread Topology."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACTION_POLL_INTERVAL,
    ACTION_TERMINAL_STATUSES,
    ACTION_TIMEOUT,
    API_MEDIA_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DIAGNOSTIC_TLV_TYPES,
    DISCOVERY_DEVICE_COUNT,
    DISCOVERY_MAX_AGE,
    DISCOVERY_MAX_RETRIES,
    DOMAIN,
    ENDPOINT_ACTIONS,
    ENDPOINT_DEVICES,
    ENDPOINT_DIAGNOSTICS,
    ENDPOINT_NODE,
    REQUEST_TIMEOUT,
    TASK_GET_DIAGNOSTIC,
    TASK_UPDATE_DEVICES,
)

_LOGGER = logging.getLogger(__name__)

CUSTOM_ROUTERS_FILE = "custom_routers.yaml"

# Known Thread Border Router OUI prefixes (first 6 chars of extended address)
# These are based on IEEE OUI database and known devices
KNOWN_BORDER_ROUTER_OUIS = {
    # Apple devices (HomePod, Apple TV)
    "28:6D:97": {"name": "Apple HomePod", "manufacturer": "Apple", "icon": "homepod"},
    "3C:22:FB": {"name": "Apple HomePod", "manufacturer": "Apple", "icon": "homepod"},
    "38:C9:86": {"name": "Apple TV", "manufacturer": "Apple", "icon": "appletv"},
    "D0:03:4B": {"name": "Apple HomePod", "manufacturer": "Apple", "icon": "homepod"},
    "F0:B3:EC": {"name": "Apple HomePod Mini", "manufacturer": "Apple", "icon": "homepod"},
    "64:B5:C6": {"name": "Apple Device", "manufacturer": "Apple", "icon": "apple"},

    # Google/Nest devices
    "18:D6:C7": {"name": "Google Nest Hub", "manufacturer": "Google", "icon": "nest"},
    "1C:F2:9A": {"name": "Google Nest", "manufacturer": "Google", "icon": "nest"},
    "20:DF:B9": {"name": "Google Nest WiFi", "manufacturer": "Google", "icon": "nest"},
    "48:D6:D5": {"name": "Google Nest Hub Max", "manufacturer": "Google", "icon": "nest"},
    "54:60:09": {"name": "Google Nest", "manufacturer": "Google", "icon": "nest"},
    "F4:F5:D8": {"name": "Google Nest", "manufacturer": "Google", "icon": "nest"},
    "F4:F5:E8": {"name": "Google Nest Mini", "manufacturer": "Google", "icon": "nest"},

    # Amazon/Eero
    "50:EC:50": {"name": "Eero Pro", "manufacturer": "Amazon/Eero", "icon": "eero"},
    "68:2A:2B": {"name": "Eero Pro 6", "manufacturer": "Amazon/Eero", "icon": "eero"},
    "70:3A:CB": {"name": "Eero", "manufacturer": "Amazon/Eero", "icon": "eero"},
    "F0:81:75": {"name": "Eero Pro 6E", "manufacturer": "Amazon/Eero", "icon": "eero"},

    # Samsung SmartThings
    "24:FC:E5": {"name": "SmartThings Hub", "manufacturer": "Samsung", "icon": "smartthings"},
    "28:6D:CD": {"name": "SmartThings Station", "manufacturer": "Samsung", "icon": "smartthings"},
    "D0:52:A8": {"name": "SmartThings Hub", "manufacturer": "Samsung", "icon": "smartthings"},

    # Nanoleaf
    "00:55:DA": {"name": "Nanoleaf Controller", "manufacturer": "Nanoleaf", "icon": "nanoleaf"},

    # Silicon Labs (often used in DIY/dev boards)
    "04:CD:15": {"name": "Silicon Labs Device", "manufacturer": "Silicon Labs", "icon": "chip"},
    "58:8E:81": {"name": "Silicon Labs Device", "manufacturer": "Silicon Labs", "icon": "chip"},
    "84:2E:14": {"name": "Silicon Labs Device", "manufacturer": "Silicon Labs", "icon": "chip"},

    # Nordic Semiconductor
    "F8:F0:05": {"name": "Nordic Device", "manufacturer": "Nordic Semiconductor", "icon": "chip"},

    # Espressif (ESP32-H2, etc.)
    "34:85:18": {"name": "ESP32 Thread", "manufacturer": "Espressif", "icon": "chip"},
    "40:22:D8": {"name": "ESP32 Thread", "manufacturer": "Espressif", "icon": "chip"},
}

# Fallback patterns for partial matches
BORDER_ROUTER_PATTERNS = [
    # Pattern, name, manufacturer
    ("EA17", "Eero", "Amazon/Eero"),
    ("EA", "Eero", "Amazon/Eero"),  # Eero addresses often end with EA17
]


def _normalize_address(address: str) -> str:
    """Normalize an extended address by stripping separators and uppercasing."""
    return address.replace(":", "").replace("-", "").replace(" ", "").upper()


def _first(data: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present (non-None) value among ``keys`` in ``data``.

    The OTBR REST API has changed key casing/naming over time, so look up a few
    candidate names and fall back to ``default``.
    """
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _is_truthy(value: Any) -> bool:
    """Coerce an OTBR mode flag to bool (the API uses either 0/1 or true/false)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


class ThreadTopologyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Thread topology data from OTBR."""

    def __init__(
        self,
        hass: HomeAssistant,
        otbr_url: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.otbr_url = otbr_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._router_index = 0  # Track router numbering
        self._custom_routers: list[dict[str, str]] = self._load_custom_routers()

    def _load_custom_routers(self) -> list[dict[str, str]]:
        """Load user-defined border routers from custom_routers.yaml."""
        config_dir = Path(__file__).parent
        yaml_path = config_dir / CUSTOM_ROUTERS_FILE

        if not yaml_path.exists():
            return []

        try:
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or "routers" not in data:
                return []

            routers = []
            for entry in data["routers"]:
                address = entry.get("address", "")
                name = entry.get("name", "Custom Router")
                manufacturer = entry.get("manufacturer", "Unknown")
                icon = entry.get("icon", "router")

                if not address:
                    _LOGGER.warning("Skipping custom router entry with no address")
                    continue

                routers.append({
                    "address": _normalize_address(address),
                    "name": name,
                    "manufacturer": manufacturer,
                    "icon": icon,
                })

            _LOGGER.info("Loaded %d custom router(s) from %s", len(routers), yaml_path)
            return routers

        except yaml.YAMLError as err:
            _LOGGER.error("Error parsing %s: %s", yaml_path, err)
            return []
        except OSError as err:
            _LOGGER.error("Error reading %s: %s", yaml_path, err)
            return []

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from OTBR API.

        The new OTBR REST layout no longer exposes a single ``GET /diagnostics``
        endpoint. Instead it is an asynchronous task queue: discover the network
        (``updateDeviceCollectionTask``), read the device collection, then request
        per-router network diagnostics (``getNetworkDiagnosticTask``) and read the
        results from the diagnostics collection.
        """
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()

            # Reset router index for each update
            self._router_index = 0

            # 1. Node / leader info
            node = await self._get_json(ENDPOINT_NODE)
            node_attrs = self._resource_attributes(node)

            # 2. Discover the network so the device collection is fresh.
            # All four attributes are required by updateDeviceCollectionTask.
            await self._run_actions(
                [
                    {
                        "type": TASK_UPDATE_DEVICES,
                        "attributes": {
                            "maxAge": DISCOVERY_MAX_AGE,
                            "maxRetries": DISCOVERY_MAX_RETRIES,
                            "deviceCount": DISCOVERY_DEVICE_COUNT,
                            "timeout": ACTION_TIMEOUT,
                        },
                    }
                ]
            )

            # 3. Read the device collection
            devices = self._resource_list(await self._get_json(ENDPOINT_DEVICES))

            # 4. Request per-router diagnostics (full mesh) in a single batch
            diagnostics_by_addr = await self._fetch_diagnostics(devices)

            # Get Matter devices and Thread Border Routers from HA device registry
            matter_devices = self._get_matter_devices()
            thread_routers = self._get_thread_border_routers()

            # 5. Process and combine data
            topology = self._process_topology(
                node_attrs, devices, diagnostics_by_addr, matter_devices, thread_routers
            )

            # Generate and save SVG to www folder
            self.save_svg_to_www(topology)

            return topology

        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise UpdateFailed(
                    "OTBR REST endpoint not found (HTTP 404). This integration "
                    "requires a recent OTBR build that exposes the /api/* REST "
                    "interface (/api/node, /api/actions, /api/devices, "
                    "/api/diagnostics)."
                ) from err
            raise UpdateFailed(f"Error communicating with OTBR: {err}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with OTBR: {err}") from err
        except asyncio.TimeoutError as err:
            raise UpdateFailed(f"Timeout communicating with OTBR: {err}") from err

    async def _get_json(self, endpoint: str) -> Any:
        """GET a JSON:API resource from the OTBR REST API."""
        url = f"{self.otbr_url}{endpoint}"
        headers = {"Accept": API_MEDIA_TYPE}
        async with self._session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            await self._raise_for_status(response, "GET", url, headers, None)
            return await response.json()

    async def _post_actions(self, tasks: list[dict[str, Any]]) -> Any:
        """POST one or more tasks to the actions queue and return the response."""
        url = f"{self.otbr_url}{ENDPOINT_ACTIONS}"
        headers = {
            "Accept": API_MEDIA_TYPE,
            "Content-Type": API_MEDIA_TYPE,
        }
        body = {"data": tasks}
        async with self._session.post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            await self._raise_for_status(response, "POST", url, headers, body)
            return await response.json()

    @staticmethod
    async def _raise_for_status(
        response: aiohttp.ClientResponse,
        method: str,
        url: str,
        request_headers: dict[str, str] | None,
        request_body: Any,
    ) -> None:
        """Raise for HTTP errors, logging the full request and response.

        OTBR returns JSON:API error details (e.g. which task attribute was
        rejected on a 422) in the body, which ``raise_for_status`` discards. On
        any 4xx/5xx the entire exchange is logged at WARNING level to make
        troubleshooting straightforward.
        """
        if response.status >= 400:
            try:
                response_body = await response.text()
            except Exception:  # noqa: BLE001 - best-effort diagnostics only
                response_body = "<unreadable response body>"
            try:
                request_body_str = (
                    json.dumps(request_body) if request_body is not None else "<none>"
                )
            except (TypeError, ValueError):
                request_body_str = repr(request_body)
            _LOGGER.warning(
                "OTBR request failed:\n"
                "--- REQUEST ---\n"
                "%s %s\n"
                "Headers: %s\n"
                "Body: %s\n"
                "--- RESPONSE ---\n"
                "HTTP %s %s\n"
                "Headers: %s\n"
                "Body: %s",
                method,
                url,
                request_headers or {},
                request_body_str,
                response.status,
                response.reason or "",
                dict(response.headers),
                response_body,
            )
        response.raise_for_status()

    async def _run_actions(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enqueue tasks and poll them until each reaches a terminal status.

        Returns the completed action items (each may carry a ``diagnostics``
        relationship pointing at an entry in the diagnostics collection).
        """
        enqueued = self._resource_list(await self._post_actions(tasks))
        action_ids = [item.get("id") for item in enqueued if item.get("id")]
        if not action_ids:
            return []

        results: list[dict[str, Any]] = []
        deadline = asyncio.get_event_loop().time() + ACTION_TIMEOUT + 5
        pending = set(action_ids)

        while pending and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(ACTION_POLL_INTERVAL)
            for action_id in list(pending):
                item = self._resource_attributes(
                    await self._get_json(f"{ENDPOINT_ACTIONS}/{action_id}"),
                    raw=True,
                )
                status = str(_first(item.get("attributes", {}), "status", default="")).lower()
                if status in ACTION_TERMINAL_STATUSES:
                    pending.discard(action_id)
                    results.append(item)

        if pending:
            _LOGGER.warning(
                "Timed out waiting for %d OTBR action(s) to complete", len(pending)
            )
        return results

    async def _fetch_diagnostics(
        self, devices: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Run getNetworkDiagnosticTask for every router and collect the results.

        Returns a mapping of normalized extended address -> diagnostic attributes.
        """
        tasks: list[dict[str, Any]] = []
        for device in devices:
            attrs = device.get("attributes", {})
            role = str(_first(attrs, "role", default="")).lower()
            if role not in ("leader", "router"):
                continue
            destination = device.get("id") or _first(attrs, "extAddress", "extaddress")
            if not destination:
                continue
            tasks.append(
                {
                    "type": TASK_GET_DIAGNOSTIC,
                    "attributes": {
                        "destination": destination,
                        "types": DIAGNOSTIC_TLV_TYPES,
                        "timeout": ACTION_TIMEOUT,
                    },
                }
            )

        if not tasks:
            return {}

        completed = await self._run_actions(tasks)

        # Resolve each completed action's diagnostics relationship to a result.
        diagnostics_by_addr: dict[str, dict[str, Any]] = {}
        for action in completed:
            diag_id = self._relationship_id(action, "diagnostics")
            if not diag_id:
                continue
            try:
                diag_attrs = self._resource_attributes(
                    await self._get_json(f"{ENDPOINT_DIAGNOSTICS}/{diag_id}")
                )
            except aiohttp.ClientError as err:
                _LOGGER.debug("Could not fetch diagnostic %s: %s", diag_id, err)
                continue
            ext = _first(diag_attrs, "extAddress", "extaddress", default="")
            if ext:
                diagnostics_by_addr[_normalize_address(ext)] = diag_attrs

        return diagnostics_by_addr

    @staticmethod
    def _resource_list(payload: Any) -> list[dict[str, Any]]:
        """Return the ``data`` array of a JSON:API collection response."""
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    @staticmethod
    def _resource_attributes(payload: Any, raw: bool = False) -> dict[str, Any]:
        """Return the resource (or its ``attributes``) from a JSON:API response.

        With ``raw=True`` the full resource object is returned (so relationships
        remain accessible); otherwise just the ``attributes`` mapping is returned.
        Falls back to the payload itself for legacy flat responses.
        """
        resource = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(resource, list):
            resource = resource[0] if resource else {}
        if not isinstance(resource, dict):
            return {}
        if raw:
            return resource
        attributes = resource.get("attributes")
        return attributes if isinstance(attributes, dict) else resource

    @staticmethod
    def _relationship_id(resource: dict[str, Any], name: str) -> str | None:
        """Extract a related resource id from a JSON:API ``relationships`` block."""
        rel = resource.get("relationships", {})
        if not isinstance(rel, dict):
            return None
        data = rel.get(name, {}).get("data") if isinstance(rel.get(name), dict) else None
        if isinstance(data, dict):
            return data.get("id")
        if isinstance(data, list) and data:
            return data[0].get("id")
        return None

    def _get_matter_devices(self) -> list[dict[str, Any]]:
        """Get Matter devices from Home Assistant device registry."""
        device_registry = dr.async_get(self.hass)
        matter_devices = []

        for device in device_registry.devices.values():
            # Check if device has matter identifier
            for identifier in device.identifiers:
                if identifier[0] == "matter":
                    # Determine transport type based on model name
                    model = (device.model or "").lower()
                    manufacturer = (device.manufacturer or "").lower()
                    name = device.name or "Unknown"

                    # Detect WiFi vs Thread transport
                    transport = "thread"  # Default to Thread
                    if "wifi" in model or "wifi" in name.lower():
                        transport = "wifi"
                    elif manufacturer in ["nuki", "wemo", "lifx"]:
                        # These typically use WiFi bridge for Matter
                        transport = "wifi"

                    matter_devices.append({
                        "name": name,
                        "model": device.model,
                        "manufacturer": device.manufacturer,
                        "identifiers": list(device.identifiers),
                        "transport": transport,
                    })
                    break

        return matter_devices

    def _get_thread_border_routers(self) -> list[dict[str, Any]]:
        """Get Thread Border Routers from Home Assistant device registry."""
        device_registry = dr.async_get(self.hass)
        routers = []

        for device in device_registry.devices.values():
            # Check for thread/otbr identifiers
            for identifier in device.identifiers:
                if identifier[0] in ("thread", "otbr", "homekit_controller"):
                    name = device.name or "Unknown"
                    manufacturer = device.manufacturer or ""

                    # Check if this looks like a border router
                    if any(kw in name.lower() for kw in ["border", "router", "hub", "homepod", "nest", "eero"]):
                        routers.append({
                            "name": name,
                            "manufacturer": manufacturer,
                            "model": device.model,
                        })
                    break

        return routers

    def _identify_router(
        self, ext_address: str, is_leader: bool, router_index: int
    ) -> dict[str, str]:
        """Identify a router by its extended address or characteristics."""
        # Check if this is the OTBR leader (typically SkyConnect or similar)
        if is_leader:
            return {
                "name": "SkyConnect (OTBR)",
                "manufacturer": "Nabu Casa",
                "type": "border_router",
                "icon": "home-assistant",
            }

        ext_normalized = _normalize_address(ext_address)

        # Check custom routers first (user-defined in custom_routers.yaml)
        for custom in self._custom_routers:
            custom_addr = custom["address"]
            # Exact full match, OUI prefix match (first 6 hex chars), or substring
            if (
                ext_normalized == custom_addr
                or (len(custom_addr) == 6 and ext_normalized[:6] == custom_addr)
                or (len(custom_addr) > 6 and custom_addr in ext_normalized)
            ):
                return {
                    "name": custom["name"],
                    "manufacturer": custom["manufacturer"],
                    "type": "border_router",
                    "icon": custom.get("icon", "router"),
                }

        # Convert extended address to OUI format (XX:XX:XX)
        if len(ext_normalized) >= 6:
            # Try different OUI formats
            oui_formats = [
                f"{ext_normalized[0:2]}:{ext_normalized[2:4]}:{ext_normalized[4:6]}",
                f"{ext_normalized[-6:-4]}:{ext_normalized[-4:-2]}:{ext_normalized[-2:]}",
            ]

            for oui in oui_formats:
                if oui in KNOWN_BORDER_ROUTER_OUIS:
                    info = KNOWN_BORDER_ROUTER_OUIS[oui]
                    return {
                        "name": info["name"],
                        "manufacturer": info["manufacturer"],
                        "type": "border_router",
                        "icon": info.get("icon", "router"),
                    }

        # Check for pattern matches in the address
        for pattern, name, manufacturer in BORDER_ROUTER_PATTERNS:
            if pattern in ext_normalized:
                return {
                    "name": name,
                    "manufacturer": manufacturer,
                    "type": "border_router",
                    "icon": "router",
                }

        # Generic fallback with numbering
        router_names = [
            ("Eero", "Amazon/Eero"),
            ("Google Nest", "Google"),
            ("Apple HomePod", "Apple"),
            ("SmartThings", "Samsung"),
            ("Thread Router", "Unknown"),
        ]

        # Cycle through router types based on index
        name, manufacturer = router_names[router_index % len(router_names)]
        if router_index > 0:
            name = f"{name} #{router_index + 1}"

        return {
            "name": name,
            "manufacturer": manufacturer,
            "type": "border_router",
            "icon": "router",
        }

    def _match_end_device(
        self, parent_rloc: int, child_idx: int, matter_devices: list[dict]
    ) -> dict[str, Any] | None:
        """Try to match an end device with a Matter device."""
        # Get Thread-only Matter devices
        thread_devices = [d for d in matter_devices if d["transport"] == "thread"]

        # Simple heuristic: assign devices based on order
        # In a real implementation, you'd need to query Matter fabric data
        if child_idx < len(thread_devices):
            return thread_devices[child_idx]

        return None

    def _process_topology(
        self,
        node_attrs: dict,
        devices: list[dict],
        diagnostics_by_addr: dict[str, dict],
        matter_devices: list[dict],
        thread_routers: list[dict],
    ) -> dict[str, Any]:
        """Process OTBR data (node + device collection + diagnostics) into topology.

        ``devices`` are JSON:API device resources from ``/api/devices`` and
        ``diagnostics_by_addr`` maps a normalized extended address to the
        diagnostic attributes returned by ``getNetworkDiagnosticTask``.
        """
        # Get leader info from the node resource attributes
        leader_ext_address = _first(node_attrs, "extAddress", "extaddress", default="")
        leader_ext_normalized = _normalize_address(leader_ext_address)
        network_name = _first(node_attrs, "networkName", "networkname", default="Unknown")
        num_routers = _first(
            node_attrs, "numOfRouter", "numberOfRouters", "routerCount", default=0
        )
        state = _first(node_attrs, "state", default="unknown")

        # Separate Thread and WiFi Matter devices
        thread_matter = [d for d in matter_devices if d["transport"] == "thread"]
        wifi_matter = [d for d in matter_devices if d["transport"] == "wifi"]

        # Build nodes dictionary (only routers/leader become nodes; children come
        # from each router's ChildTable, mirroring the old per-router diagnostics)
        nodes: dict[str, dict] = {}
        thread_device_idx = 0
        router_index = 0

        for device in devices:
            attrs = device.get("attributes", {})
            ext_address = device.get("id") or _first(
                attrs, "extAddress", "extaddress", default=""
            )
            if not ext_address:
                continue

            ext_normalized = _normalize_address(ext_address)
            rloc16 = _first(attrs, "rloc16", default=0)

            # Determine device role from the device collection
            role_raw = str(_first(attrs, "role", default="")).lower()
            is_leader = ext_normalized == leader_ext_normalized or "leader" in role_raw
            is_router = is_leader or "router" in role_raw

            # Children attach to their router via the ChildTable, so skip
            # non-router devices as standalone nodes.
            if not is_router:
                continue

            role = "leader" if is_leader else "router"

            # Per-router diagnostics (may be empty if the query timed out)
            diag = diagnostics_by_addr.get(ext_normalized, {})

            # Get router identification
            router_info = self._identify_router(ext_address, is_leader, router_index)
            router_index += 1

            # Get connectivity info from diagnostics
            connectivity = _first(diag, "connectivity", default={}) or {}
            leader_cost = _first(connectivity, "leaderCost", default=0)

            # Get best link quality (3 = best, 0 = none)
            lq3 = _first(connectivity, "linkQuality3", default=0)
            lq2 = _first(connectivity, "linkQuality2", default=0)
            lq1 = _first(connectivity, "linkQuality1", default=0)

            if lq3 > 0:
                link_quality = 3
            elif lq2 > 0:
                link_quality = 2
            elif lq1 > 0:
                link_quality = 1
            else:
                link_quality = 0

            # Get children and try to match with Matter devices
            child_table = _first(diag, "childTable", default=[]) or []
            children = []
            for child in child_table:
                child_id = _first(child, "childId", default=0)
                child_mode = _first(child, "mode", default={}) or {}
                rx_on_idle = _first(child_mode, "rxOnWhenIdle", default=True)
                child_type = "active" if _is_truthy(rx_on_idle) else "sleepy"

                # Try to match with a Matter device
                matter_match = None
                if thread_device_idx < len(thread_matter):
                    matter_match = thread_matter[thread_device_idx]
                    thread_device_idx += 1

                child_info = {
                    "id": child_id,
                    "type": child_type,
                    "timeout": _first(child, "timeout", default=0),
                    "rloc16": _first(child, "rloc16", default=rloc16 + child_id),
                }

                if matter_match:
                    child_info["name"] = matter_match["name"]
                    child_info["manufacturer"] = matter_match["manufacturer"]
                    child_info["model"] = matter_match["model"]

                children.append(child_info)

            # Get route data for mesh connections (route64 TLV)
            route = _first(diag, "route64", default={}) or {}
            route_data = _first(route, "routeData", "routes", default=[]) or []
            connections = []
            for rd in route_data:
                if _first(rd, "routeCost", default=255) < 255:
                    connections.append({
                        "router_id": _first(rd, "routerId", default=0),
                        "lq_out": _first(rd, "linkQualityOut", default=0),
                        "lq_in": _first(rd, "linkQualityIn", default=0),
                        "cost": _first(rd, "routeCost", default=0),
                    })

            nodes[ext_address] = {
                "ext_address": ext_address,
                "rloc16": rloc16,
                "role": role,
                "name": router_info["name"],
                "manufacturer": router_info["manufacturer"],
                "device_type": router_info["type"],
                "icon": router_info.get("icon", "router"),
                "link_quality": link_quality,
                "leader_cost": leader_cost,
                "children": children,
                "child_count": len(children),
                "connections": connections,
                "ip_addresses": _first(
                    diag, "ipv6AddressList", "ipv6Addresses", default=[]
                ) or [],
            }

        return {
            "network_name": network_name,
            "state": state,
            "leader_address": leader_ext_address,
            "router_count": num_routers,
            "nodes": nodes,
            "total_devices": len(nodes) + sum(n["child_count"] for n in nodes.values()),
            "matter_devices": {
                "thread": thread_matter,
                "wifi": wifi_matter,
                "total": len(matter_devices),
            },
            "known_routers": thread_routers,
        }

    def generate_svg(self, topology: dict[str, Any]) -> str:
        """Generate an SVG visualization of the Thread network topology."""
        width = 800
        height = 700

        nodes = topology.get("nodes", {})
        network_name = topology.get("network_name", "Thread Network")
        router_count = topology.get("router_count", 0)
        total_devices = topology.get("total_devices", 0)
        matter_data = topology.get("matter_devices", {})
        thread_matter = matter_data.get("thread", [])
        wifi_matter = matter_data.get("wifi", [])

        # Separate nodes by role
        leader = None
        routers = []
        for ext_addr, node in nodes.items():
            if node["role"] == "leader":
                leader = node
            elif node["role"] == "router":
                routers.append(node)

        # SVG header and styles
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="4" stdDeviation="8" flood-opacity="0.3"/>
    </filter>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
      <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <linearGradient id="cardGrad" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" style="stop-color:#2d2d2d"/><stop offset="100%" style="stop-color:#1a1a1a"/>
    </linearGradient>
    <linearGradient id="leaderGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#ffd700"/><stop offset="100%" style="stop-color:#ff8c00"/>
    </linearGradient>
    <linearGradient id="routerGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#03a9f4"/><stop offset="100%" style="stop-color:#0277bd"/>
    </linearGradient>
    <linearGradient id="threadGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#00bcd4"/><stop offset="100%" style="stop-color:#006064"/>
    </linearGradient>
    <linearGradient id="wifiGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#9c27b0"/><stop offset="100%" style="stop-color:#6a1b9a"/>
    </linearGradient>
    <style>
      .card {{ fill: url(#cardGrad); }}
      .title {{ fill: #ffffff; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 22px; font-weight: 600; }}
      .subtitle {{ fill: #9e9e9e; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 14px; }}
      .stat-value {{ fill: #ffffff; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 28px; font-weight: 700; }}
      .stat-label {{ fill: #757575; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }}
      .node-label {{ fill: #ffffff; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 12px; font-weight: 500; }}
      .node-sublabel {{ fill: #9e9e9e; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 10px; }}
      .device-label {{ fill: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 11px; }}
      .section-title {{ fill: #ffffff; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 14px; font-weight: 600; }}
      .connection {{ stroke: #00bcd4; stroke-width: 2; fill: none; opacity: 0.6; }}
      .connection-mesh {{ stroke: #03a9f4; stroke-width: 1.5; stroke-dasharray: 8,4; fill: none; opacity: 0.4; }}
    </style>
  </defs>

  <!-- Card background -->
  <rect class="card" x="0" y="0" width="{width}" height="{height}" rx="16" ry="16" filter="url(#shadow)"/>

  <!-- Header Section -->
  <text class="title" x="30" y="45">🧵 Thread Network Topology</text>
  <text class="subtitle" x="30" y="68">{network_name} • Real-time network visualization</text>

  <!-- Stats Row -->
  <g transform="translate(30, 90)">
    <rect x="0" y="0" width="120" height="70" rx="10" fill="#333" opacity="0.5"/>
    <text class="stat-value" x="60" y="38" text-anchor="middle">{router_count}</text>
    <text class="stat-label" x="60" y="55" text-anchor="middle">Border Routers</text>

    <rect x="140" y="0" width="120" height="70" rx="10" fill="#333" opacity="0.5"/>
    <text class="stat-value" x="200" y="38" text-anchor="middle">{total_devices}</text>
    <text class="stat-label" x="200" y="55" text-anchor="middle">Thread Devices</text>

    <rect x="280" y="0" width="120" height="70" rx="10" fill="#00696b" opacity="0.3"/>
    <text class="stat-value" x="340" y="38" text-anchor="middle" fill="#00bcd4">{len(thread_matter)}</text>
    <text class="stat-label" x="340" y="55" text-anchor="middle" fill="#00838f">Matter Thread</text>

    <rect x="420" y="0" width="120" height="70" rx="10" fill="#4a148c" opacity="0.3"/>
    <text class="stat-value" x="480" y="38" text-anchor="middle" fill="#ce93d8">{len(wifi_matter)}</text>
    <text class="stat-label" x="480" y="55" text-anchor="middle" fill="#8e24aa">Matter WiFi</text>
  </g>

  <!-- Divider -->
  <line x1="30" y1="175" x2="770" y2="175" stroke="#333" stroke-width="1"/>
'''

        # Calculate positions for nodes
        leader_x, leader_y = 400, 230
        router_positions = []
        num_routers = len(routers)

        if num_routers > 0:
            router_spacing = min(200, 600 // (num_routers + 1))
            start_x = 400 - (num_routers - 1) * router_spacing // 2
            for i in range(num_routers):
                router_positions.append((start_x + i * router_spacing, 340))

        # Draw connections (Leader to Routers)
        if leader:
            for i, pos in enumerate(router_positions):
                svg += f'  <path class="connection" d="M {leader_x} {leader_y + 20} Q {(leader_x + pos[0])//2} {(leader_y + pos[1])//2 + 20} {pos[0]} {pos[1] - 25}"/>\n'

        # Draw mesh connections between routers
        for i in range(len(router_positions) - 1):
            x1, y1 = router_positions[i]
            x2, y2 = router_positions[i + 1]
            svg += f'  <path class="connection-mesh" d="M {x1 + 30} {y1} Q {(x1 + x2)//2} {y1 + 30} {x2 - 30} {y2}"/>\n'

        # Draw Leader node
        if leader:
            lq = leader.get("link_quality", 3)
            lq_text = ["Poor", "Fair", "Good", "Excellent"][min(lq, 3)]
            svg += f'''
  <!-- LEADER NODE -->
  <g transform="translate({leader_x}, {leader_y})" filter="url(#glow)">
    <circle cx="0" cy="0" r="45" fill="url(#leaderGrad)" opacity="0.2"/>
    <circle cx="0" cy="0" r="35" fill="url(#leaderGrad)"/>
    <text x="0" y="8" text-anchor="middle" font-size="28">👑</text>
  </g>
  <text class="node-label" x="{leader_x}" y="{leader_y + 60}" text-anchor="middle">{leader["name"]}</text>
  <text class="node-sublabel" x="{leader_x}" y="{leader_y + 74}" text-anchor="middle">{leader["manufacturer"]} • Leader • LQ: {lq_text}</text>
'''
            # Draw Leader's children
            children = leader.get("children", [])
            if children:
                child_start_x = leader_x - (len(children) - 1) * 40
                for j, child in enumerate(children):
                    cx = child_start_x + j * 80
                    cy = leader_y + 130
                    child_name = child.get("name", f"Device {child.get('id', j)}")
                    child_type = child.get("type", "active")
                    emoji = "💤" if child_type == "sleepy" else "🔋"

                    svg += f'  <path class="connection" d="M {leader_x} {leader_y + 45} L {cx} {cy - 20}" opacity="0.4"/>\n'
                    svg += f'''  <g transform="translate({cx}, {cy})">
    <circle cx="0" cy="0" r="22" fill="url(#threadGrad)" opacity="0.15"/>
    <circle cx="0" cy="0" r="16" fill="url(#threadGrad)"/>
    <text x="0" y="5" text-anchor="middle" font-size="14">{emoji}</text>
  </g>
  <text class="device-label" x="{cx}" y="{cy + 30}" text-anchor="middle">{child_name[:20]}</text>
'''

        # Draw Router nodes
        for i, router in enumerate(routers):
            if i >= len(router_positions):
                break
            rx, ry = router_positions[i]
            lq = router.get("link_quality", 3)
            lq_text = ["Poor", "Fair", "Good", "Excellent"][min(lq, 3)]

            svg += f'''
  <!-- ROUTER {i+1} -->
  <g transform="translate({rx}, {ry})">
    <circle cx="0" cy="0" r="32" fill="url(#routerGrad)" opacity="0.2"/>
    <circle cx="0" cy="0" r="25" fill="url(#routerGrad)"/>
    <text x="0" y="7" text-anchor="middle" font-size="20">📡</text>
  </g>
  <text class="node-label" x="{rx}" y="{ry + 42}" text-anchor="middle">{router["name"]}</text>
  <text class="node-sublabel" x="{rx}" y="{ry + 55}" text-anchor="middle">{router["manufacturer"]} • Router • LQ: {lq_text}</text>
'''
            # Draw Router's children
            children = router.get("children", [])
            if children:
                child_start_x = rx - (len(children) - 1) * 35
                for j, child in enumerate(children):
                    cx = child_start_x + j * 70
                    cy = ry + 120
                    child_name = child.get("name", f"Device {child.get('id', j)}")
                    child_type = child.get("type", "active")
                    emoji = "💤" if child_type == "sleepy" else "🔋"

                    svg += f'  <path class="connection" d="M {rx} {ry + 30} L {cx} {cy - 20}" opacity="0.4"/>\n'
                    svg += f'''  <g transform="translate({cx}, {cy})">
    <circle cx="0" cy="0" r="22" fill="url(#threadGrad)" opacity="0.15"/>
    <circle cx="0" cy="0" r="16" fill="url(#threadGrad)"/>
    <text x="0" y="5" text-anchor="middle" font-size="14">{emoji}</text>
  </g>
  <text class="device-label" x="{cx}" y="{cy + 30}" text-anchor="middle">{child_name[:18]}</text>
'''

        # WiFi section
        wifi_y = 580
        svg += f'''
  <!-- Divider -->
  <line x1="30" y1="{wifi_y - 30}" x2="770" y2="{wifi_y - 30}" stroke="#333" stroke-width="1"/>

  <!-- WiFi Section -->
  <text class="section-title" x="30" y="{wifi_y}">📶 Matter over WiFi</text>
'''
        # WiFi devices
        for i, device in enumerate(wifi_matter[:4]):  # Max 4 devices
            dx = 60 + i * 180
            svg += f'''  <g transform="translate({dx}, {wifi_y + 40})">
    <rect x="-40" y="-25" width="150" height="50" rx="8" fill="url(#wifiGrad)" opacity="0.2"/>
    <text x="0" y="-2" font-size="16">🔌</text>
    <text class="device-label" x="25" y="-2">{device["name"][:16]}</text>
    <text class="node-sublabel" x="25" y="12">{device.get("manufacturer", "")[:16]}</text>
  </g>
'''

        # Legend
        svg += f'''
  <!-- Legend -->
  <g transform="translate(550, {wifi_y - 10})">
    <text class="node-sublabel" x="0" y="0">LEGEND</text>
    <circle cx="15" cy="20" r="8" fill="url(#leaderGrad)"/>
    <text class="node-sublabel" x="30" y="24">Leader</text>
    <circle cx="85" cy="20" r="8" fill="url(#routerGrad)"/>
    <text class="node-sublabel" x="100" y="24">Router</text>
    <circle cx="165" cy="20" r="8" fill="url(#threadGrad)"/>
    <text class="node-sublabel" x="180" y="24">End Device</text>
  </g>

  <!-- Connection Legend -->
  <g transform="translate(550, {wifi_y + 35})">
    <line x1="0" y1="10" x2="40" y2="10" stroke="#00bcd4" stroke-width="2" opacity="0.6"/>
    <text class="node-sublabel" x="50" y="14">Parent-Child</text>
    <line x1="130" y1="10" x2="170" y2="10" stroke="#03a9f4" stroke-width="1.5" stroke-dasharray="8,4" opacity="0.4"/>
    <text class="node-sublabel" x="180" y="14">Mesh</text>
  </g>
'''
        svg += '</svg>'
        return svg

    def save_svg_to_www(self, topology: dict[str, Any]) -> str | None:
        """Generate SVG and save to www folder."""
        try:
            svg_content = self.generate_svg(topology)
            www_path = self.hass.config.path("www")

            # Create www folder if it doesn't exist
            if not os.path.exists(www_path):
                os.makedirs(www_path)

            svg_path = os.path.join(www_path, "thread_topology.svg")
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg_content)

            _LOGGER.debug("SVG saved to %s", svg_path)
            return "/local/thread_topology.svg"
        except Exception as err:
            _LOGGER.error("Failed to save SVG: %s", err)
            return None

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        if self._session:
            await self._session.close()
            self._session = None
