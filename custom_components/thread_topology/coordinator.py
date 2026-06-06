"""Data coordinator for Thread Topology."""
from __future__ import annotations

import asyncio
import json
import logging
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


def _parse_rloc16(value: Any) -> int | None:
    """Parse an rloc16 to int. The API returns it as a hex string (``"0x3c00"``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def _link_quality_from_margin(margin: Any) -> int:
    """Map a link margin (dB) to a Thread link quality (0-3).

    ``routerNeighbors`` reports a neighbour's link as a margin in dB rather than
    a 0-3 quality, so convert it using OpenThread's standard thresholds (link
    quality 1/2/3 at >= 2/10/20 dB).
    """
    try:
        m = float(margin)
    except (TypeError, ValueError):
        return 0
    if m >= 20:
        return 3
    if m >= 10:
        return 2
    if m >= 2:
        return 1
    return 0


def _ext_to_hex(value: Any) -> str | None:
    """Normalize a Matter extended address to a 16-char lowercase hex string.

    ``ThreadNetworkDiagnostics.ExtAddress`` is a uint64, while a
    ``GeneralDiagnostics`` hardware address is an 8-byte octet string; accept
    either (and the already-hex string form) and reject anything that isn't a
    full 64-bit address.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:016x}" if value > 0 else None
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        return b.hex() if len(b) == 8 else None
    if isinstance(value, str):
        norm = _normalize_address(value)
        return norm.lower() if len(norm) == 16 else None
    return None


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

        The new OTBR REST layout is an asynchronous task queue:

        1. ``GET /api/node`` for node/leader info.
        2. ``updateDeviceCollectionTask`` to discover/refresh the network.
        3. Per-router ``getNetworkDiagnosticTask`` queries (addressed by rloc16)
           to refresh the mesh diagnostics, then read the ``/api/diagnostics``
           collection (the per-router route table and child table live there).
        """
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()

            # Reset router index for each update
            self._router_index = 0

            # 1. Node / leader info
            node_attrs = self._resource_attributes(await self._get_json(ENDPOINT_NODE))

            # 2. Discover the network so the collections are fresh (best effort).
            await self._refresh_device_collection()

            # 3. Refresh + read per-router diagnostics (the live mesh)
            diagnostics = await self._fetch_diagnostics(node_attrs)

            # Get Matter devices and Thread Border Routers from HA device registry
            matter_devices = self._get_matter_devices()
            thread_routers = self._get_thread_border_routers()

            # 4. Process and combine data
            topology = self._process_topology(
                node_attrs, diagnostics, matter_devices, thread_routers
            )

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

    async def _refresh_device_collection(self) -> None:
        """Trigger network discovery so the device/diagnostics collections refresh.

        Best effort: a failure here should not abort the whole update because the
        collections retain their last-known contents.
        """
        try:
            await self._run_action(
                {
                    "type": TASK_UPDATE_DEVICES,
                    "attributes": {
                        "maxAge": DISCOVERY_MAX_AGE,
                        "maxRetries": DISCOVERY_MAX_RETRIES,
                        "deviceCount": DISCOVERY_DEVICE_COUNT,
                        "timeout": ACTION_TIMEOUT,
                    },
                }
            )
        except aiohttp.ClientError as err:
            _LOGGER.warning("OTBR device discovery failed: %s", err)

    async def _fetch_diagnostics(
        self, node_attrs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Refresh and return the per-router network diagnostics collection.

        ``getNetworkDiagnosticTask`` only accepts an rloc16 destination on this
        OTBR build (an extAddress/deviceId destination returns 422). The router
        rloc16 set is derived from the current diagnostics collection plus the
        node's own rloc16 and the leader, a refresh is requested for each (best
        effort), and the refreshed ``/api/diagnostics`` collection is returned.
        """
        diagnostics = self._resource_list(await self._get_json(ENDPOINT_DIAGNOSTICS))
        rlocs = self._router_rlocs(node_attrs, diagnostics)

        action_ids: list[str] = []
        for rloc in rlocs:
            task = {
                "type": TASK_GET_DIAGNOSTIC,
                "attributes": {
                    "destination": f"0x{rloc:04x}",
                    "types": DIAGNOSTIC_TLV_TYPES,
                    "timeout": ACTION_TIMEOUT,
                },
            }
            try:
                action_id = await self._enqueue_action(task)
            except aiohttp.ClientError as err:
                _LOGGER.warning(
                    "Diagnostic request for 0x%04x failed, skipping: %s", rloc, err
                )
                continue
            if action_id:
                action_ids.append(action_id)

        if action_ids:
            await self._await_actions(action_ids)
            diagnostics = self._resource_list(
                await self._get_json(ENDPOINT_DIAGNOSTICS)
            )

        return diagnostics

    @staticmethod
    def _router_rlocs(
        node_attrs: dict[str, Any], diagnostics: list[dict[str, Any]]
    ) -> list[int]:
        """Derive the set of router rloc16 values to query for diagnostics.

        A router's rloc16 is ``routerId << 10`` (child bits zeroed). Candidates
        come from the node's own rloc16, the leader (``leaderRouterId``), and
        every router referenced by an existing diagnostic entry or its route
        table, so a single populated entry seeds the whole router set.
        """
        rlocs: set[int] = set()

        own = _parse_rloc16(_first(node_attrs, "rloc16", default=None))
        if own is not None:
            rlocs.add(own & 0xFC00)

        leader_data = node_attrs.get("leaderData", {}) or {}
        leader_rid = _first(leader_data, "leaderRouterId", default=None)
        if isinstance(leader_rid, int):
            rlocs.add((leader_rid << 10) & 0xFFFF)

        for diag in diagnostics:
            attrs = diag.get("attributes", {})
            r = _parse_rloc16(_first(attrs, "rloc16", default=None))
            if r is not None:
                rlocs.add(r & 0xFC00)
            route = _first(attrs, "route", "route64", default={}) or {}
            for rd in _first(route, "routeData", "routes", default=[]) or []:
                rid = _first(rd, "routeId", "routerId", default=None)
                if isinstance(rid, int):
                    rlocs.add((rid << 10) & 0xFFFF)

        return sorted(rlocs)

    async def _enqueue_action(self, task: dict[str, Any]) -> str | None:
        """POST a single task to the actions queue and return its action id."""
        enqueued = self._resource_list(await self._post_actions([task]))
        for item in enqueued:
            if item.get("id"):
                return item["id"]
        return None

    async def _run_action(self, task: dict[str, Any]) -> None:
        """Enqueue a single task and wait for it to reach a terminal status."""
        action_id = await self._enqueue_action(task)
        if action_id:
            await self._await_actions([action_id])

    async def _await_actions(self, action_ids: list[str]) -> None:
        """Poll the given actions until each reaches a terminal status."""
        deadline = asyncio.get_event_loop().time() + ACTION_TIMEOUT + 5
        pending = set(action_ids)

        while pending and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(ACTION_POLL_INTERVAL)
            for action_id in list(pending):
                try:
                    item = self._resource_attributes(
                        await self._get_json(f"{ENDPOINT_ACTIONS}/{action_id}"),
                        raw=True,
                    )
                except aiohttp.ClientError:
                    pending.discard(action_id)
                    continue
                status = str(
                    _first(item.get("attributes", {}), "status", default="")
                ).lower()
                if status in ACTION_TERMINAL_STATUSES:
                    pending.discard(action_id)

        if pending:
            _LOGGER.warning(
                "Timed out waiting for %d OTBR action(s) to complete", len(pending)
            )

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

    def _get_matter_devices(self) -> list[dict[str, Any]]:
        """Get Matter devices from Home Assistant, enriched with Thread address.

        The device registry only stores the Matter node id + name, so the Thread
        extended address / rloc16 (needed to map a device to its OTBR entry) is
        read from the Matter integration's node data (the same source as the
        device page's "Matter info" panel). All of that is best-effort: if the
        Matter integration isn't present or its internals change, devices simply
        come back without an address and the topology falls back to neutral names.
        """
        device_registry = dr.async_get(self.hass)
        matter_devices = []

        for device in device_registry.devices.values():
            # Check if device has matter identifier
            for identifier in device.identifiers:
                if identifier[0] == "matter":
                    model = (device.model or "").lower()
                    manufacturer = (device.manufacturer or "").lower()
                    # Prefer the user-assigned name (what's shown in the HA UI);
                    # device.name is the integration default (usually the model),
                    # so 5 renamed "MYGGBETT" sensors stay distinguishable.
                    name = (
                        getattr(device, "name_by_user", None)
                        or device.name
                        or "Unknown"
                    )

                    entry = {
                        "name": name,
                        "model": device.model,
                        "manufacturer": device.manufacturer,
                        "identifiers": list(device.identifiers),
                        "transport": None,
                        "ext_address": None,
                        "ext_addresses": [],
                    }

                    # Enrich with Thread address/role from the Matter node
                    self._enrich_matter_device(device, entry)

                    # Fall back to a name/model heuristic if the Matter node did
                    # not tell us the transport.
                    if entry["transport"] is None:
                        if "wifi" in model or "wifi" in name.lower() or manufacturer in (
                            "nuki",
                            "wemo",
                            "lifx",
                        ):
                            entry["transport"] = "wifi"
                        else:
                            entry["transport"] = "thread"

                    matter_devices.append(entry)
                    break

        return matter_devices

    def _enrich_matter_device(self, device: Any, entry: dict[str, Any]) -> None:
        """Best-effort: read the Thread extended address(es) + transport.

        Collects every extended address a device might present so it can be
        matched to its OTBR entry regardless of which one OTBR reports:

        - ``ThreadNetworkDiagnostics.ExtAddress`` is the *operational* extended
          address the device actually uses in the mesh. Devices that randomise
          their extended address (rather than deriving it from the factory
          EUI-64) report this value to OTBR, so it is the reliable join key and
          is preferred for display.
        - ``GeneralDiagnostics.NetworkInterfaces[].hardwareAddress`` is the
          factory hardware address; kept as a secondary key for devices that use
          it directly as their Thread address.

        ``entry["ext_addresses"]`` holds all candidates (the matcher tries each);
        ``entry["ext_address"]`` is the preferred one. Wrapped in broad guards
        because this reaches into another integration.

        Emits a per-device DEBUG record of every raw value read (the operational
        ``ExtAddress``, each network interface, and the derived candidates) so a
        failed match can be traced to its cause — most often a sleepy end device
        whose operational ``ExtAddress`` read times out, leaving only the factory
        hardware address (which never appears on the mesh).
        """
        label = entry.get("name", "Unknown")
        try:
            from homeassistant.components.matter.helpers import (
                get_node_from_device_entry,
            )
            from chip.clusters import Objects as clusters
        except Exception as err:  # noqa: BLE001 - matter/chip may be absent
            _LOGGER.debug(
                "Matter enrich %s: matter/chip import failed (%s: %s)",
                label, type(err).__name__, err,
            )
            return

        try:
            node = get_node_from_device_entry(self.hass, device)
        except Exception as err:  # noqa: BLE001 - internal API, stay defensive
            _LOGGER.debug(
                "Matter enrich %s: get_node_from_device_entry raised (%s: %s)",
                label, type(err).__name__, err,
            )
            node = None
        if node is None:
            _LOGGER.debug(
                "Matter enrich %s: no Matter node found for device "
                "(identifiers=%s) — cannot read any extended address",
                label, entry.get("identifiers"),
            )
            return

        candidates: list[str] = []

        def _add(addr: str | None) -> None:
            if addr and addr not in candidates:
                candidates.append(addr)

        # ThreadNetworkDiagnostics.ExtAddress -> operational extended address
        # (the value OTBR reports). Preferred, so collected first.
        op_exc: str | None = None
        try:
            op_ext = node.get_attribute_value(
                0,
                clusters.ThreadNetworkDiagnostics,
                clusters.ThreadNetworkDiagnostics.Attributes.ExtAddress,
            )
        except Exception as err:  # noqa: BLE001
            op_ext = None
            op_exc = f"{type(err).__name__}: {err}"
        op_hex = _ext_to_hex(op_ext)
        if op_hex:
            _add(op_hex)
            entry["transport"] = "thread"
        _LOGGER.debug(
            "Matter enrich %s: ThreadNetworkDiagnostics.ExtAddress raw=%r "
            "(type=%s) -> hex=%s%s",
            label, op_ext, type(op_ext).__name__, op_hex,
            f" [read raised {op_exc}]" if op_exc else "",
        )

        # GeneralDiagnostics network interfaces -> hardware address + transport
        ifaces_exc: str | None = None
        try:
            interfaces = node.get_attribute_value(
                0,
                clusters.GeneralDiagnostics,
                clusters.GeneralDiagnostics.Attributes.NetworkInterfaces,
            ) or []
        except Exception as err:  # noqa: BLE001
            interfaces = []
            ifaces_exc = f"{type(err).__name__}: {err}"

        iface_log: list[str] = []
        for iface in interfaces:
            hw = getattr(iface, "hardwareAddress", None)
            itype = getattr(iface, "type", None)
            is_op = getattr(iface, "isOperational", True)
            hw_bytes = bytes(hw) if hw else b""
            iface_log.append(
                f"(name={getattr(iface, 'name', None)!r} type={itype} "
                f"operational={is_op} hw={hw_bytes.hex() or None})"
            )
            if not is_op:
                continue
            # Thread interface type is 4; a Thread MAC is an 8-byte EUI-64
            if itype == 4 or len(hw_bytes) == 8:
                _add(hw_bytes.hex())
                entry["transport"] = "thread"
                break
            if itype in (1, 2):  # WiFi / Ethernet
                entry["transport"] = "wifi"
        _LOGGER.debug(
            "Matter enrich %s: GeneralDiagnostics.NetworkInterfaces=%s%s",
            label, ", ".join(iface_log) or "[]",
            f" [read raised {ifaces_exc}]" if ifaces_exc else "",
        )

        if candidates:
            entry["ext_addresses"] = candidates
            entry["ext_address"] = candidates[0]

        _LOGGER.debug(
            "Matter enrich %s: candidates=%s transport=%s (preferred=%s)",
            label, candidates or None, entry.get("transport"),
            entry.get("ext_address"),
        )

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
        self,
        ext_address: str,
        router_index: int,
        matter_name: str | None = None,
        vendor_name: str | None = None,
        vendor_model: str | None = None,
    ) -> dict[str, str]:
        """Identify a router by its extended address or characteristics.

        ``matter_name`` is the Home Assistant device name when this router is a
        known Matter device (matched by extended address). ``vendor_name`` /
        ``vendor_model`` come from the device's own diagnostic vendor TLVs and
        are used when Home Assistant doesn't know the device.
        """
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

        # A Home Assistant Matter device matched by extended address
        if matter_name:
            return {
                "name": matter_name,
                "manufacturer": "Matter",
                "type": "border_router",
                "icon": "router",
            }

        # The device's own vendor info (e.g. a standalone OpenThread BR that
        # Home Assistant doesn't know as a Matter device)
        vendor_name = (vendor_name or "").strip()
        vendor_model = (vendor_model or "").strip()
        if vendor_name or vendor_model:
            label = " ".join(part for part in (vendor_name, vendor_model) if part)
            return {
                "name": label,
                "manufacturer": vendor_name or "Unknown",
                "type": "border_router",
                "icon": "router",
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

        # Neutral fallback: identify by the last 4 hex of the address rather than
        # inventing a brand. Use custom_routers.yaml to assign real names.
        short = ext_normalized[-4:] if len(ext_normalized) >= 4 else ext_normalized
        return {
            "name": f"Thread Router ({short})",
            "manufacturer": "Unknown",
            "type": "border_router",
            "icon": "router",
        }

    def _process_topology(
        self,
        node_attrs: dict,
        diagnostics: list[dict],
        matter_devices: list[dict],
        thread_routers: list[dict],
    ) -> dict[str, Any]:
        """Process OTBR data (node + diagnostics collection) into topology.

        Router nodes come from the live ``/api/diagnostics`` entries — the
        routers actually present/responding in the mesh, matching what the OTBR
        web UI graphs. (The cached ``/api/devices`` collection is deliberately
        NOT used: it retains stale entries for devices that have left the
        network, which would show as phantom routers.) The leader is the router
        whose ``routerId`` equals the node's ``leaderData.leaderRouterId``.

        Children come from each router's ``children`` diagnostic (which includes
        each child's extended address), so they are matched to Home Assistant
        Matter devices by extended address and named just like routers. Builds
        that only return the legacy ``childTable`` (no address) leave children
        unnamed rather than guessing.
        """
        network_name = _first(node_attrs, "networkName", "networkname", default="Unknown")
        state = _first(node_attrs, "state", default="unknown")
        leader_data = node_attrs.get("leaderData", {}) or {}
        leader_router_id = _first(leader_data, "leaderRouterId", default=None)
        self_ext = _normalize_address(
            _first(node_attrs, "extAddress", "extaddress", default="")
        )

        # Separate Thread and WiFi Matter devices (informational only)
        thread_matter = [d for d in matter_devices if d["transport"] == "thread"]
        wifi_matter = [d for d in matter_devices if d["transport"] == "wifi"]

        # Map to name Thread devices from Home Assistant Matter data, keyed by
        # extended address (the "MAC" on the device's Matter info panel). A device
        # may present more than one candidate address (operational vs hardware),
        # so index every candidate; the same keys name both routers and children.
        matter_by_ext: dict[str, str] = {}
        for d in matter_devices:
            exts = d.get("ext_addresses") or (
                [d["ext_address"]] if d.get("ext_address") else []
            )
            for ext in exts:
                if ext:
                    matter_by_ext[_normalize_address(ext)] = d["name"]

        # Index diagnostics by normalized extended address, and build the router
        # set from the live diagnostics entries only. The collection holds many
        # snapshots per router; the *freshest* (last) snapshot is authoritative
        # for the router's identity, children and naming, because older snapshots
        # carry stale children (e.g. roaming sleepy devices, double-counted across
        # parents) and lack the extAddress needed to name them.
        #
        # Connectivity is tracked separately (``conn_by_ext``): a router's freshest
        # snapshot can be degraded (no route table, empty neighbour list) while an
        # earlier one still has its mesh links, so for links/link-quality fall back
        # to the most recent snapshot that actually carries them. This keeps live
        # routers from rendering isolated without resurrecting their stale children.
        diag_by_ext: dict[str, dict] = {}
        router_exts: dict[str, str] = {}
        conn_by_ext: dict[str, dict] = {}
        for diag in diagnostics:
            attrs = diag.get("attributes", {})
            ext = _first(attrs, "extAddress", "extaddress", default="") or diag.get("id", "")
            if not ext:
                continue
            norm = _normalize_address(ext)
            diag_by_ext[norm] = attrs
            router_exts[norm] = ext
            if _first(attrs, "route", "route64") or attrs.get("routerNeighbors"):
                conn_by_ext[norm] = attrs

        def _router_id(attrs: dict, rloc_int: int) -> int | None:
            rid = _first(attrs, "routerId", default=None)
            if isinstance(rid, int):
                return rid
            return rloc_int >> 10 if rloc_int else None

        # Resolve each router's diagnostics, rloc16 and routerId
        resolved = []
        for norm_ext, ext_address in router_exts.items():
            diag = diag_by_ext.get(norm_ext, {})
            rloc_int = _parse_rloc16(_first(diag, "rloc16", default=None)) or 0
            router_id = _router_id(diag, rloc_int)
            # Prefer the diagnostic's own isLeader flag; fall back to routerId
            is_leader = _is_truthy(diag.get("isLeader")) or (
                leader_router_id is not None and router_id == leader_router_id
            )
            resolved.append((norm_ext, ext_address, diag, rloc_int, router_id, is_leader))

        # Sort leader first, then by rloc16, for stable ordering
        resolved.sort(key=lambda r: (0 if r[5] else 1, r[3]))

        nodes: dict[str, dict] = {}
        router_index = 0
        leader_ext_address = ""

        for norm_ext, ext_address, diag, rloc_int, router_id, is_leader in resolved:
            if is_leader:
                leader_ext_address = ext_address
            role = "leader" if is_leader else "router"

            # The node behind /api/node is the border router this integration is
            # connected to. It is NOT necessarily the Home Assistant radio (the
            # HA OTBR build doesn't expose the full /api/* diagnostics, so a
            # separate OTBR is usually queried), so just flag it, don't name it.
            is_queried_otbr = norm_ext == self_ext and bool(self_ext)
            router_info = self._identify_router(
                ext_address,
                router_index,
                matter_by_ext.get(norm_ext),
                _first(diag, "vendorName", default=""),
                _first(diag, "vendorModel", default=""),
            )
            router_index += 1

            # Connectivity (route table + MAC neighbours) is read from the most
            # recent snapshot that actually carries it, which may be older than the
            # freshest snapshot used for everything else above.
            conn_diag = conn_by_ext.get(norm_ext, diag)

            # Route table (the build names this TLV "route"; "route64" on others)
            route = _first(conn_diag, "route", "route64", default={}) or {}
            route_data = _first(route, "routeData", "routes", default=[]) or []

            # MAC neighbour table: this build (and some routers whose full route
            # TLV never comes back) report their live mesh links here, as a link
            # margin in dB rather than a 0-3 quality.
            router_neighbors = _first(conn_diag, "routerNeighbors", default=[]) or []

            # Link quality: prefer a connectivity TLV when present, otherwise
            # derive it from the best inbound link to a neighbouring router.
            connectivity = _first(conn_diag, "connectivity", default={}) or {}
            leader_cost = _first(connectivity, "leaderCost", default=0)
            if connectivity:
                lq3 = _first(connectivity, "linkQuality3", default=0)
                lq2 = _first(connectivity, "linkQuality2", default=0)
                lq1 = _first(connectivity, "linkQuality1", default=0)
                link_quality = 3 if lq3 > 0 else 2 if lq2 > 0 else 1 if lq1 > 0 else 0
            elif route_data:
                neighbour_lq = [
                    _first(rd, "linkQualityIn", default=0)
                    for rd in route_data
                    if _first(rd, "routeId", "routerId", default=None) != router_id
                ]
                link_quality = min(max(neighbour_lq), 3) if neighbour_lq else 0
            elif router_neighbors:
                margins = [
                    _first(n, "linkMargin", default=0) for n in router_neighbors
                ]
                link_quality = _link_quality_from_margin(max(margins))
            else:
                # No diagnostics for this router -> link quality is unknown
                link_quality = None

            # Children: prefer the "children" diagnostic (has each child's
            # extAddress); fall back to the legacy childTable (childId only).
            child_entries = _first(diag, "children", default=None)
            use_children_tlv = child_entries is not None
            if not use_children_tlv:
                child_entries = _first(diag, "childTable", default=[]) or []

            children = []
            for child in child_entries:
                child_id = _first(child, "childId", default=0)

                # rxOnWhenIdle is top-level in "children", under "mode" in childTable
                if use_children_tlv:
                    rx_on_idle = _first(child, "rxOnWhenIdle", default=True)
                else:
                    child_mode = _first(child, "mode", default={}) or {}
                    rx_on_idle = _first(child_mode, "rxOnWhenIdle", default=True)
                child_type = "active" if _is_truthy(rx_on_idle) else "sleepy"

                # A child's rloc16 is the parent router rloc16 with the child id
                # in the low bits.
                child_rloc = _parse_rloc16(_first(child, "rloc16", default=None))
                if child_rloc is None:
                    child_rloc = (rloc_int & 0xFC00) | child_id

                child_ext = _first(child, "extAddress", "extaddress", default="")

                child_info = {
                    "id": child_id,
                    "type": child_type,
                    "timeout": _first(child, "timeout", default=0),
                    "rloc16": child_rloc,
                    "ext_address": child_ext,
                }
                # Name the child by matching its extended address to a Home
                # Assistant Matter device.
                if child_ext:
                    child_name = matter_by_ext.get(_normalize_address(child_ext))
                    if child_name:
                        child_info["name"] = child_name
                children.append(child_info)

            # Mesh connections from the route table (skip the self entry)
            connections = []
            for rd in route_data:
                rid = _first(rd, "routeId", "routerId", default=None)
                if rid is None or rid == router_id:
                    continue
                if _first(rd, "routeCost", default=255) < 255:
                    connections.append({
                        "router_id": rid,
                        "lq_out": _first(rd, "linkQualityOut", default=0),
                        "lq_in": _first(rd, "linkQualityIn", default=0),
                        "cost": _first(rd, "routeCost", default=0),
                    })

            # Supplement with the MAC neighbour table so routers whose route TLV
            # is absent (or which the route table omits) still show their links
            # instead of rendering as isolated. routerNeighbors only reports the
            # inbound link (margin measured locally), so use it for both
            # directions; the reciprocal router fills in the rest.
            covered = {c["router_id"] for c in connections}
            for neighbor in router_neighbors:
                n_rloc = _parse_rloc16(_first(neighbor, "rloc16", default=None))
                if n_rloc is None:
                    continue
                n_rid = n_rloc >> 10
                if n_rid == router_id or n_rid in covered:
                    continue
                lq = _link_quality_from_margin(
                    _first(neighbor, "linkMargin", default=0)
                )
                connections.append({
                    "router_id": n_rid,
                    "lq_out": lq,
                    "lq_in": lq,
                    "cost": 0,
                })
                covered.add(n_rid)

            nodes[ext_address] = {
                "ext_address": ext_address,
                "rloc16": rloc_int,
                "role": role,
                "is_otbr": is_queried_otbr,
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

        # Surface any device-identification gaps for debugging (Thread nodes that
        # don't match a Home Assistant device, and vice versa).
        self._log_identification_gaps(nodes, thread_matter)

        return {
            "network_name": network_name,
            "state": state,
            "leader_address": leader_ext_address,
            "router_count": len(nodes),
            "nodes": nodes,
            "total_devices": len(nodes) + sum(n["child_count"] for n in nodes.values()),
            "matter_devices": {
                "thread": thread_matter,
                "wifi": wifi_matter,
                "total": len(matter_devices),
            },
            "known_routers": thread_routers,
        }

    @staticmethod
    def _log_identification_gaps(
        nodes: dict[str, dict], thread_matter: list[dict]
    ) -> None:
        """Warn when Thread nodes and HA Matter devices don't line up.

        Naming relies on matching a Thread node's extended address (the "MAC" on
        the Matter info panel) to a Home Assistant Matter device's extended
        address. When a node ends up unidentified it is almost always because the
        two sides don't share an extended address, so log both directions of the
        mismatch — plus the cases where a match is structurally impossible (a
        Thread child with no extAddress, or an HA device whose Matter enrichment
        produced no extAddress) — with the rloc16/extAddress needed to chase it
        through the OTBR ``/api/diagnostics`` REST API.
        """
        # Thread side: routers + children that carry an extended address.
        # ``identified_routers`` are routers already named by vendor/OUI/custom
        # data (e.g. border routers) — they aren't Matter devices and aren't the
        # "unidentified" case, so they're tracked (to keep the reverse direction
        # correct) but excluded from the gap list.
        thread_by_ext: dict[str, str] = {}
        identified_routers: set[str] = set()
        children_without_ext: list[str] = []
        for node in nodes.values():
            r_ext_raw = node.get("ext_address", "")
            r_ext = _normalize_address(r_ext_raw)
            name = node.get("name", "") or ""
            if r_ext:
                thread_by_ext[r_ext] = (
                    f"{r_ext_raw} (router, rloc16 0x{node.get('rloc16', 0):04x}, "
                    f"role {node.get('role')}, shown as '{name}')"
                )
                if not name.startswith("Thread Router ("):
                    identified_routers.add(r_ext)
            for child in node.get("children", []):
                c_ext_raw = child.get("ext_address", "")
                c_ext = _normalize_address(c_ext_raw)
                where = (
                    f"child rloc16 0x{child.get('rloc16', 0):04x} "
                    f"({child.get('type')}) under router "
                    f"0x{node.get('rloc16', 0):04x}"
                )
                if c_ext:
                    thread_by_ext[c_ext] = f"{c_ext_raw} ({where})"
                else:
                    children_without_ext.append(where)

        # Home Assistant side: Matter-over-Thread devices. A device may present
        # several candidate addresses (operational vs hardware); it matches the
        # mesh if any candidate is present, and all candidates are shown so a true
        # mismatch can be debugged against OTBR.
        ha_addrs: list[tuple[str, list[str]]] = []  # (name, normalized candidates)
        ha_without_ext: list[str] = []
        for device in thread_matter:
            name = device.get("name", "Unknown")
            exts = device.get("ext_addresses") or (
                [device["ext_address"]] if device.get("ext_address") else []
            )
            cands = [_normalize_address(e) for e in exts if e]
            if cands:
                ha_addrs.append((name, cands))
            else:
                ha_without_ext.append(name)

        ha_by_ext: dict[str, str] = {
            ext: name for name, cands in ha_addrs for ext in cands
        }

        thread_only = [
            desc for ext, desc in sorted(thread_by_ext.items())
            if ext not in ha_by_ext and ext not in identified_routers
        ]
        ha_only = [
            f"{name} (extAddress {'/'.join(cands)})"
            for name, cands in sorted(ha_addrs)
            if not any(ext in thread_by_ext for ext in cands)
        ]

        if not (thread_only or ha_only or children_without_ext or ha_without_ext):
            return

        lines = [
            "Thread topology device identification gaps "
            "(nodes are matched to Home Assistant by extended address):"
        ]
        if thread_only:
            lines.append(
                f"  On Thread but NOT matched to a Home Assistant device "
                f"({len(thread_only)}):"
            )
            lines += [f"    - {item}" for item in thread_only]
        if ha_only:
            lines.append(
                f"  In Home Assistant (Matter/Thread) but NOT found on the mesh "
                f"({len(ha_only)}):"
            )
            lines += [f"    - {item}" for item in ha_only]
        if children_without_ext:
            lines.append(
                f"  Thread children with no extAddress, so they can never be "
                f"matched (the OTBR 'children' TLV was missing for their parent; "
                f"only the legacy childTable was returned) "
                f"({len(children_without_ext)}):"
            )
            lines += [f"    - {item}" for item in children_without_ext]
        if ha_without_ext:
            lines.append(
                f"  Home Assistant Matter/Thread devices with no extended address, "
                f"so they can never be matched (Matter enrichment failed — the "
                f"GeneralDiagnostics network interface was unreadable) "
                f"({len(ha_without_ext)}):"
            )
            lines += [f"    - {item}" for item in ha_without_ext]

        _LOGGER.warning("\n".join(lines))

    def generate_tree(self, topology: dict[str, Any]) -> str:
        """Build a monospace ASCII tree diagram of the topology.

        Returned wrapped in a fenced code block so Home Assistant's built-in
        Markdown card renders it preformatted (aligned, no whitespace collapse).
        Unlike the previous fixed-coordinate SVG this never overlaps, and unlike
        Mermaid it needs no custom card. Use it from a Markdown card via
        ``{{ state_attr('sensor.thread_topology_map', 'topology_text') }}``.
        """
        nodes = topology.get("nodes", {})
        network_name = topology.get("network_name", "Thread Network")
        leader_addr = topology.get("leader_address", "")
        router_count = topology.get("router_count", 0)
        total_devices = topology.get("total_devices", 0)
        matter = topology.get("matter_devices", {})
        wifi_matter = matter.get("wifi", [])
        lq_text = ["Poor", "Fair", "Good", "Excellent"]

        lines = ["```text"]
        lines.append(
            f"\U0001f9f5 {network_name}   "
            f"({router_count} routers · {total_devices} devices)"
        )

        if not nodes:
            lines.append("")
            lines.append("(no routers found)")
            lines.append("```")
            return "\n".join(lines)

        # Stable ordering: leader first, then routers by rloc16
        ordered = sorted(
            nodes.items(),
            key=lambda kv: (0 if kv[0] == leader_addr else 1, kv[1].get("rloc16", 0)),
        )

        for ext, node in ordered:
            role = node.get("role", "router")
            emoji = "\U0001f451" if role == "leader" else "\U0001f4e1"
            role_label = "Leader" if role == "leader" else "Router"
            lq_value = node.get("link_quality")
            lq = lq_text[min(lq_value, 3)] if isinstance(lq_value, int) else "Unknown"
            # Mark the border router this integration is connected to
            otbr_tag = "  ·  \U0001f310 connected OTBR" if node.get("is_otbr") else ""
            # Always show the 4-digit hex node number (rloc16), even for routers
            # already named from Home Assistant.
            rloc_hex = f"0x{node.get('rloc16', 0):04x}"
            lines.append("")
            lines.append(
                f"{emoji} {node.get('name', 'Router')} ({rloc_hex})  ·  "
                f"{role_label}  ·  LQ {lq}{otbr_tag}"
            )
            children = node.get("children", [])
            for i, child in enumerate(children):
                branch = "└─" if i == len(children) - 1 else "├─"
                cemoji = "\U0001f4a4" if child.get("type") == "sleepy" else "\U0001f50b"
                # Prefer the HA name; fall back to a neutral "Device" label.
                cname = child.get("name") or "Device"
                # Always show the 4-digit hex node number (rloc16), even for
                # children already named from Home Assistant.
                crloc_hex = f"0x{child.get('rloc16', 0):04x}"
                lines.append(f"{branch} {cemoji} {cname} ({crloc_hex})")

        # Inter-router mesh links (the router↔router edges the graph tools draw).
        # Each router's route table reports the link to every neighbour, so the
        # same edge is seen from both ends; key by the unordered rloc16 pair and
        # keep the first sighting. ``lq_out``/``lq_in`` are relative to the node
        # that reported the edge (``a``): out = a→b, in = b→a.
        links: dict[frozenset[int], dict[str, int]] = {}
        for ext, node in ordered:
            a_rloc = node.get("rloc16", 0)
            for conn in node.get("connections", []):
                b_rloc = (conn.get("router_id", 0) << 10) & 0xFFFF
                key = frozenset((a_rloc, b_rloc))
                if len(key) < 2 or key in links:
                    continue
                links[key] = {
                    "a": a_rloc,
                    "b": b_rloc,
                    "lq_out": conn.get("lq_out", 0),
                    "lq_in": conn.get("lq_in", 0),
                    "cost": conn.get("cost", 0),
                }

        if links:
            lines.append("")
            lines.append("\U0001f517 Mesh links  (LQ a→b / b→a)")
            for link in sorted(links.values(), key=lambda lk: (lk["a"], lk["b"])):
                a_hex = f"0x{link['a']:04x}"
                b_hex = f"0x{link['b']:04x}"
                lines.append(
                    f"   {a_hex} ↔ {b_hex}  ·  "
                    f"LQ {link['lq_out']}/{link['lq_in']}  ·  cost {link['cost']}"
                )

        if wifi_matter:
            lines.append("")
            lines.append("\U0001f4f6 Matter over WiFi")
            for device in wifi_matter:
                manufacturer = device.get("manufacturer", "")
                suffix = f" ({manufacturer})" if manufacturer else ""
                lines.append(f"• {device.get('name', 'Device')}{suffix}")

        lines.append("```")
        return "\n".join(lines)

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        if self._session:
            await self._session.close()
            self._session = None
