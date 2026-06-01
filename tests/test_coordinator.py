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

    def test_child_rloc16_computed(self, topology):
        # child rloc16 = parent rloc16 (0x1c00) with child id in low bits
        leader = topology["nodes"][LEADER]
        child6 = next(c for c in leader["children"] if c["id"] == 6)
        assert child6["rloc16"] == (0x1C00 | 6)

    def test_matter_split(self, topology):
        assert len(topology["matter_devices"]["thread"]) == 3
        assert len(topology["matter_devices"]["wifi"]) == 2
        assert topology["matter_devices"]["total"] == 5

    def test_mermaid_generation(self, topology):
        coordinator = _build_coordinator()
        mermaid = coordinator.generate_mermaid(topology)
        # Fenced mermaid block so a Markdown card renders it in-browser
        assert mermaid.startswith("```mermaid")
        assert mermaid.rstrip().endswith("```")
        assert "flowchart TD" in mermaid
        # Leader node + a mesh edge + at least one child edge are present
        assert "👑" in mermaid
        assert "---" in mermaid  # leader-to-router mesh link
        assert "-->" in mermaid  # router-to-child link

    def test_mermaid_empty_network(self):
        coordinator = _build_coordinator()
        mermaid = coordinator.generate_mermaid({"nodes": {}, "network_name": "Empty"})
        assert mermaid.startswith("```mermaid")
        assert "No routers found" in mermaid


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
