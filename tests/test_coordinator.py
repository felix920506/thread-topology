"""Tests for Thread Topology coordinator."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock
import pytest

# Mock homeassistant modules so coordinator can be imported without HA installed
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.const", MagicMock())
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.device_registry", MagicMock())
sys.modules.setdefault("homeassistant.helpers.update_coordinator", MagicMock())

import custom_components.thread_topology.coordinator as coordinator_module
from custom_components.thread_topology.coordinator import (
    _normalize_address,
    _parse_rloc16,
    KNOWN_BORDER_ROUTER_OUIS,
    ThreadTopologyCoordinator,
)

LEADER = "228942D83C99F228"  # routerId 7 == leaderRouterId
ROUTER_A = "7690F04AB3B4E9DA"  # routerId 15 (the queried node)
ROUTER_B = "4E6BC0581D23D773"  # routerId 61


def _build_coordinator() -> ThreadTopologyCoordinator:
    """Create a coordinator instance with HA dependencies mocked out."""
    return ThreadTopologyCoordinator(MagicMock(), "http://localhost:8081")


@pytest.fixture
def topology(
    mock_otbr_node_response,
    mock_otbr_diagnostics_response,
    mock_matter_devices,
):
    """Run the real _process_topology over the JSON:API fixtures."""
    coordinator = _build_coordinator()
    node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
    diagnostics = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
    return coordinator._process_topology(
        node_attrs, diagnostics, mock_matter_devices, []
    )


class TestResourceHelpers:
    """Test cases for JSON:API response helpers."""

    def test_resource_attributes_single(self, mock_otbr_node_response):
        attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        assert attrs["networkName"] == "MyHome1038137341"
        assert attrs["extAddress"] == ROUTER_A

    def test_resource_attributes_raw_keeps_relationships(self):
        payload = {
            "data": {
                "id": "abc",
                "attributes": {"status": "completed"},
                "relationships": {"result": {"data": {"type": "diagnostics", "id": ""}}},
            }
        }
        raw = ThreadTopologyCoordinator._resource_attributes(payload, raw=True)
        assert raw["id"] == "abc"
        assert raw["attributes"]["status"] == "completed"

    def test_resource_list_collection(self, mock_otbr_diagnostics_response):
        items = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
        assert len(items) == 3
        assert items[0]["id"] == "diag-1"


class TestMatterDeviceNames:
    """Matter devices should use the user-assigned name, not the model."""

    def _fake_device(self, **kw):
        dev = MagicMock()
        dev.identifiers = {("matter", "deviceid_x")}
        dev.model = kw.get("model", "MYGGBETT")
        dev.manufacturer = kw.get("manufacturer", "IKEA")
        dev.name = kw.get("name", "MYGGBETT door/window sensor")
        dev.name_by_user = kw.get("name_by_user")
        return dev

    def _devices(self, monkeypatch, dev):
        reg = MagicMock()
        reg.devices = {"x": dev}
        monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: reg)
        coord = _build_coordinator()
        coord.hass = MagicMock()  # not set by the fake coordinator base
        return coord._get_matter_devices()

    def test_prefers_user_name(self, monkeypatch):
        dev = self._fake_device(name_by_user="Front Door")
        assert self._devices(monkeypatch, dev)[0]["name"] == "Front Door"

    def test_falls_back_to_device_name(self, monkeypatch):
        dev = self._fake_device(name_by_user=None)
        assert self._devices(monkeypatch, dev)[0]["name"] == "MYGGBETT door/window sensor"


class TestParseRloc16:
    """Test cases for the rloc16 parser (the API returns hex strings)."""

    def test_hex_string(self):
        assert _parse_rloc16("0x3c00") == 0x3C00
        assert _parse_rloc16("0x1c00") == 7168

    def test_int_passthrough(self):
        assert _parse_rloc16(8192) == 8192

    def test_invalid(self):
        assert _parse_rloc16("nope") is None
        assert _parse_rloc16(None) is None
        assert _parse_rloc16(True) is None


class TestRouterRlocs:
    """Test deriving the router rloc16 set to query for diagnostics."""

    def test_derives_all_routers(
        self, mock_otbr_node_response, mock_otbr_diagnostics_response
    ):
        node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        diagnostics = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
        rlocs = ThreadTopologyCoordinator._router_rlocs(node_attrs, diagnostics)
        # routerIds 7, 15, 61 -> rloc16 0x1c00, 0x3c00, 0xf400
        assert set(rlocs) == {0x1C00, 0x3C00, 0xF400}

    def test_seeds_from_leader_when_diagnostics_empty(self, mock_otbr_node_response):
        node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        rlocs = ThreadTopologyCoordinator._router_rlocs(node_attrs, [])
        # own rloc16 (0x3c00) and leaderRouterId 7 (<<10 = 0x1c00)
        assert set(rlocs) == {0x3C00, 0x1C00}


class TestProcessTopology:
    """Test cases for the end-to-end topology processing."""

    def test_network_metadata(self, topology):
        assert topology["network_name"] == "MyHome1038137341"
        assert topology["router_count"] == 3

    def test_leader_is_router_with_matching_router_id(self, topology):
        # The queried node is a router; the leader is identified via routerId.
        assert topology["leader_address"] == LEADER
        assert topology["nodes"][LEADER]["role"] == "leader"

    def test_node_count(self, topology):
        assert len(topology["nodes"]) == 3

    def test_total_devices(self, topology):
        # 3 routers + 4 children = 7
        assert topology["total_devices"] == 7

    def test_router_roles(self, topology):
        assert topology["nodes"][ROUTER_A]["role"] == "router"
        assert topology["nodes"][ROUTER_B]["role"] == "router"

    def test_link_quality_derived_from_route(self, topology):
        # No connectivity TLV -> derived from best inbound neighbour link (3)
        for node in topology["nodes"].values():
            assert node["link_quality"] == 3

    def test_child_counts(self, topology):
        counts = {addr: node["child_count"] for addr, node in topology["nodes"].items()}
        assert counts[LEADER] == 2
        assert counts[ROUTER_A] == 1
        assert counts[ROUTER_B] == 1

    def test_sleepy_vs_active_children(self, topology):
        node = topology["nodes"][LEADER]
        types = sorted(child["type"] for child in node["children"])
        # rxOnWhenIdle False -> sleepy, True -> active
        assert types == ["active", "sleepy"]

    def test_connections_exclude_self(self, topology):
        leader = topology["nodes"][LEADER]
        router_ids = {c["router_id"] for c in leader["connections"]}
        # leader is routerId 7; connections are to 15 and 61, never itself
        assert router_ids == {15, 61}

    def test_router_with_only_neighbors_is_not_isolated(
        self, mock_otbr_node_response
    ):
        """A router whose only diagnostic data is routerNeighbors still links up.

        Mirrors live router 0x2400: its freshest snapshot has no route TLV (and
        the other routers' route tables omit it), so the route table yields zero
        connections. The routerNeighbors MAC table must be used as a fallback so
        it isn't drawn as isolated.
        """
        coordinator = _build_coordinator()
        node_attrs = ThreadTopologyCoordinator._resource_attributes(
            mock_otbr_node_response
        )
        diagnostics = [
            {
                "attributes": {
                    "extAddress": "3ECD5E8D98957D8A",
                    "rloc16": "0x2400",  # routerId 9
                    "routerId": 9,
                    # No route TLV; only the MAC neighbour table.
                    "routerNeighbors": [
                        {"rloc16": "0x1c00", "linkMargin": 10},  # -> LQ 2
                        {"rloc16": "0xf400", "linkMargin": 25},  # -> LQ 3
                    ],
                }
            }
        ]
        topo = coordinator._process_topology(node_attrs, diagnostics, [], [])
        node = topo["nodes"]["3ECD5E8D98957D8A"]
        conns = {c["router_id"]: c for c in node["connections"]}
        assert set(conns) == {7, 61}  # 0x1c00 -> rid 7, 0xf400 -> rid 61
        assert conns[7]["lq_in"] == 2
        assert conns[61]["lq_in"] == 3
        # link quality is derived from the best neighbour margin, not Unknown
        assert node["link_quality"] == 3

    def test_richest_snapshot_wins_over_freshest(self, mock_otbr_node_response):
        """A degraded later snapshot must not shadow an earlier complete one.

        The OTBR diagnostics collection accumulates many snapshots per router; a
        router's most recent one can be empty (no route, no neighbours). De-dup
        must keep the richest snapshot, not the last one.
        """
        coordinator = _build_coordinator()
        node_attrs = ThreadTopologyCoordinator._resource_attributes(
            mock_otbr_node_response
        )
        diagnostics = [
            {
                "attributes": {
                    "extAddress": "3ECD5E8D98957D8A",
                    "rloc16": "0x2400",
                    "routerId": 9,
                    "routerNeighbors": [{"rloc16": "0xf400", "linkMargin": 25}],
                }
            },
            {
                # A newer but degraded snapshot for the same router.
                "attributes": {
                    "extAddress": "3ECD5E8D98957D8A",
                    "rloc16": "0x2400",
                    "routerId": 9,
                    "routerNeighbors": [],
                }
            },
        ]
        topo = coordinator._process_topology(node_attrs, diagnostics, [], [])
        node = topo["nodes"]["3ECD5E8D98957D8A"]
        # The richer snapshot's neighbour (0xf400 -> rid 61) survives.
        assert {c["router_id"] for c in node["connections"]} == {61}

    def test_child_rloc16_computed(self, topology):
        # child rloc16 = parent rloc16 (0x1c00) with child id in low bits
        leader = topology["nodes"][LEADER]
        child6 = next(c for c in leader["children"] if c["id"] == 6)
        assert child6["rloc16"] == (0x1C00 | 6)

    def test_router_named_from_vendor(self, topology):
        # ROUTER_B reports vendor info and isn't a HA Matter device, so it's
        # named from vendorName + vendorModel.
        assert (
            topology["nodes"][ROUTER_B]["name"]
            == "Home Assistant OpenThread Border Router"
        )

    def test_children_are_unnamed(self, topology):
        # With no Matter data, children must NOT be given a guessed name.
        for node in topology["nodes"].values():
            for child in node["children"]:
                assert "name" not in child

    def test_router_named_from_matter_ext_address(
        self, mock_otbr_node_response, mock_otbr_diagnostics_response
    ):
        """A router matched to a HA Matter device by extended address is named."""
        coordinator = _build_coordinator()
        node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        diagnostics = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
        matter = [
            {
                "name": "IKEA ALPSTUGA",
                "transport": "thread",
                "ext_address": "4e6bc0581d23d773",  # ROUTER_B, lowercase
                "rloc16": None,
            }
        ]
        topo = coordinator._process_topology(node_attrs, diagnostics, matter, [])
        assert topo["nodes"][ROUTER_B]["name"] == "IKEA ALPSTUGA"

    def test_child_named_from_matter_ext_address(
        self, mock_otbr_node_response, mock_otbr_diagnostics_response
    ):
        """A child from the 'children' TLV is named by matching its extAddress."""
        coordinator = _build_coordinator()
        node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        diagnostics = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
        # Leader child id 6 has extAddress AAAA000000000006 in the fixture
        matter = [
            {"name": "Aqara Sensor", "transport": "thread", "ext_address": "aaaa000000000006"}
        ]
        topo = coordinator._process_topology(node_attrs, diagnostics, matter, [])
        named = [
            c.get("name")
            for c in topo["nodes"][LEADER]["children"]
            if c["id"] == 6
        ]
        assert named == ["Aqara Sensor"]

    def test_matter_split(self, topology):
        assert len(topology["matter_devices"]["thread"]) == 3
        assert len(topology["matter_devices"]["wifi"]) == 2
        assert topology["matter_devices"]["total"] == 5

    def test_stale_device_not_shown_as_router(
        self, mock_otbr_node_response, mock_otbr_diagnostics_response
    ):
        """Routers come only from live diagnostics; stale device-collection
        entries with no diagnostics must NOT appear (regression: phantom 55C5)."""
        coordinator = _build_coordinator()
        node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        diagnostics = ThreadTopologyCoordinator._resource_list(mock_otbr_diagnostics_response)
        topo = coordinator._process_topology(node_attrs, diagnostics, [], [])
        # Only the 3 routers that have diagnostics entries
        assert len(topo["nodes"]) == 3
        assert "E20796B3C5CE55C5" not in topo["nodes"]

    def test_tree_generation(self, topology):
        coordinator = _build_coordinator()
        tree = coordinator.generate_tree(topology)
        # Fenced code block so a Markdown card renders it monospace/aligned
        assert tree.startswith("```text")
        assert tree.rstrip().endswith("```")
        assert "MyHome1038137341" in tree
        assert "👑" in tree  # leader
        assert "📡" in tree  # router
        assert "└─" in tree  # child branch
        # 4-digit hex node number (rloc16) shown for every node, even named ones
        assert "0x3c00" in tree  # leader rloc16
        # Inter-router mesh links (router↔router edges) are rendered, de-duped to
        # one line per pair, with directional LQ and route cost.
        assert "Mesh links" in tree
        assert "0x1c00 ↔ 0x3c00" in tree
        assert "0x1c00 ↔ 0xf400" in tree
        assert "0x3c00 ↔ 0xf400" in tree
        assert tree.count(" ↔ ") == 3  # three unique edges, not six directed
        assert "cost 1" in tree

    def test_tree_empty_network(self):
        coordinator = _build_coordinator()
        tree = coordinator.generate_tree({"nodes": {}, "network_name": "Empty"})
        assert tree.startswith("```text")
        assert "no routers found" in tree


class TestBorderRouterIdentification:
    """Test cases for border router identification."""

    def test_eero_pattern_matching(self):
        """Test Eero router identification by pattern."""
        BORDER_ROUTER_PATTERNS = [
            ("EA17", "Eero", "Amazon/Eero"),
            ("EA", "Eero", "Amazon/Eero"),
        ]

        ext_address = "96308C2577D6EA17"

        matched = None
        for pattern, name, manufacturer in BORDER_ROUTER_PATTERNS:
            if pattern in ext_address.upper():
                matched = (name, manufacturer)
                break

        assert matched is not None
        assert matched[0] == "Eero"
        assert matched[1] == "Amazon/Eero"

    def test_oui_based_identification(self):
        """Test OUI-based router identification."""
        KNOWN_OUIS = {
            "28:6D:97": {"name": "Apple HomePod", "manufacturer": "Apple"},
            "18:D6:C7": {"name": "Google Nest Hub", "manufacturer": "Google"},
            "50:EC:50": {"name": "Eero Pro", "manufacturer": "Amazon/Eero"},
        }

        ext = "286D970123456789"
        oui = f"{ext[0:2]}:{ext[2:4]}:{ext[4:6]}"

        assert oui in KNOWN_OUIS
        assert KNOWN_OUIS[oui]["manufacturer"] == "Apple"


class TestMatterDeviceMatching:
    """Test cases for Matter device matching."""

    def test_thread_device_filter(self, mock_matter_devices):
        thread_devices = [d for d in mock_matter_devices if d["transport"] == "thread"]
        assert len(thread_devices) == 3

    def test_wifi_device_filter(self, mock_matter_devices):
        wifi_devices = [d for d in mock_matter_devices if d["transport"] == "wifi"]
        assert len(wifi_devices) == 2

    def test_device_name_access(self, mock_matter_devices):
        names = [d["name"] for d in mock_matter_devices]
        assert "Meross MS605" in names
        assert "Nuki Smart Lock" in names


class TestNormalizeAddress:
    """Test cases for address normalization."""

    def test_strips_colons(self):
        assert _normalize_address("AA:BA:D1:1C:1D:3A:F2:7F") == "AABAD11C1D3AF27F"

    def test_strips_dashes(self):
        assert _normalize_address("AA-BA-D1") == "AABAD1"

    def test_uppercases(self):
        assert _normalize_address("aabad1") == "AABAD1"

    def test_strips_spaces(self):
        assert _normalize_address("AA BA D1") == "AABAD1"

    def test_already_normalized(self):
        assert _normalize_address("AABAD11C1D3AF27F") == "AABAD11C1D3AF27F"


class TestCustomRouterMatching:
    """Test cases for custom router YAML matching."""

    def test_exact_full_address_match(self):
        custom_routers = [
            {"address": "AABAD11C1D3AF27F", "name": "SMlight", "manufacturer": "SMlight", "icon": "chip"}
        ]
        ext_normalized = _normalize_address("AABAD11C1D3AF27F")

        matched = None
        for custom in custom_routers:
            if ext_normalized == custom["address"]:
                matched = custom
                break

        assert matched is not None
        assert matched["name"] == "SMlight"

    def test_oui_prefix_match(self):
        custom_routers = [
            {"address": "AABAD1", "name": "SMlight", "manufacturer": "SMlight", "icon": "chip"}
        ]
        ext_normalized = "AABAD11C1D3AF27F"

        matched = None
        for custom in custom_routers:
            custom_addr = custom["address"]
            if len(custom_addr) == 6 and ext_normalized[:6] == custom_addr:
                matched = custom
                break

        assert matched is not None
        assert matched["name"] == "SMlight"

    def test_substring_pattern_match(self):
        custom_routers = [
            {"address": "121BEC66", "name": "ESP32-H2", "manufacturer": "Espressif", "icon": "chip"}
        ]
        ext_normalized = "121BEC66640787A6"

        matched = None
        for custom in custom_routers:
            custom_addr = custom["address"]
            if len(custom_addr) > 6 and custom_addr in ext_normalized:
                matched = custom
                break

        assert matched is not None
        assert matched["name"] == "ESP32-H2"

    def test_no_match_returns_none(self):
        custom_routers = [
            {"address": "FF0011", "name": "Unknown", "manufacturer": "Unknown", "icon": "chip"}
        ]
        ext_normalized = "AABAD11C1D3AF27F"

        matched = None
        for custom in custom_routers:
            custom_addr = custom["address"]
            if ext_normalized == custom_addr:
                matched = custom
            elif len(custom_addr) == 6 and ext_normalized[:6] == custom_addr:
                matched = custom
            elif len(custom_addr) > 6 and custom_addr in ext_normalized:
                matched = custom

        assert matched is None

    def test_custom_routers_priority_over_builtin(self):
        ext = "286D970123456789"
        custom_routers = [
            {"address": "286D97", "name": "My Custom Router", "manufacturer": "Custom", "icon": "router"}
        ]
        ext_normalized = _normalize_address(ext)

        custom_match = None
        for custom in custom_routers:
            custom_addr = custom["address"]
            if len(custom_addr) == 6 and ext_normalized[:6] == custom_addr:
                custom_match = custom
                break

        assert custom_match is not None
        assert custom_match["name"] == "My Custom Router"

        oui = f"{ext_normalized[0:2]}:{ext_normalized[2:4]}:{ext_normalized[4:6]}"
        assert oui in KNOWN_BORDER_ROUTER_OUIS
        assert KNOWN_BORDER_ROUTER_OUIS[oui]["manufacturer"] == "Apple"


class TestURLNormalization:
    """Test cases for URL normalization."""

    def test_trailing_slash_removal(self):
        urls = [
            ("http://localhost:8081/", "http://localhost:8081"),
            ("http://localhost:8081", "http://localhost:8081"),
            ("http://homeassistant.local:8081/", "http://homeassistant.local:8081"),
        ]

        for input_url, expected in urls:
            result = input_url.rstrip("/")
            assert result == expected

    def test_endpoint_construction(self):
        base_url = "http://localhost:8081"
        endpoint = "/api/node"

        full_url = f"{base_url}{endpoint}"

        assert full_url == "http://localhost:8081/api/node"
