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
    KNOWN_BORDER_ROUTER_OUIS,
    ThreadTopologyCoordinator,
)


def _build_coordinator() -> ThreadTopologyCoordinator:
    """Create a coordinator instance with HA dependencies mocked out."""
    return ThreadTopologyCoordinator(MagicMock(), "http://localhost:8081")


@pytest.fixture
def topology(
    mock_otbr_node_response,
    mock_otbr_devices_response,
    mock_diagnostics_by_addr,
    mock_matter_devices,
):
    """Run the real _process_topology over the JSON:API fixtures."""
    coordinator = _build_coordinator()
    node_attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
    devices = ThreadTopologyCoordinator._resource_list(mock_otbr_devices_response)
    return coordinator._process_topology(
        node_attrs, devices, mock_diagnostics_by_addr, mock_matter_devices, []
    )


class TestResourceHelpers:
    """Test cases for JSON:API response helpers."""

    def test_resource_attributes_single(self, mock_otbr_node_response):
        attrs = ThreadTopologyCoordinator._resource_attributes(mock_otbr_node_response)
        assert attrs["networkName"] == "MyHome1038137341"
        assert attrs["extAddress"] == "1EA5312CFB153F0B"

    def test_resource_attributes_raw_keeps_relationships(self):
        payload = {
            "data": {
                "id": "abc",
                "attributes": {"status": "completed"},
                "relationships": {"diagnostics": {"data": {"id": "diag-1"}}},
            }
        }
        raw = ThreadTopologyCoordinator._resource_attributes(payload, raw=True)
        assert raw["id"] == "abc"
        assert raw["relationships"]["diagnostics"]["data"]["id"] == "diag-1"

    def test_resource_list_collection(self, mock_otbr_devices_response):
        items = ThreadTopologyCoordinator._resource_list(mock_otbr_devices_response)
        assert len(items) == 4
        assert items[0]["id"] == "1EA5312CFB153F0B"

    def test_relationship_id(self):
        resource = {
            "id": "action-1",
            "relationships": {"diagnostics": {"data": {"type": "x", "id": "diag-42"}}},
        }
        assert ThreadTopologyCoordinator._relationship_id(resource, "diagnostics") == "diag-42"

    def test_relationship_id_missing(self):
        assert ThreadTopologyCoordinator._relationship_id({}, "diagnostics") is None


class TestProcessTopology:
    """Test cases for the end-to-end topology processing."""

    def test_network_metadata(self, topology):
        assert topology["network_name"] == "MyHome1038137341"
        assert topology["state"] == "leader"
        assert topology["leader_address"] == "1EA5312CFB153F0B"
        assert topology["router_count"] == 3

    def test_only_routers_become_nodes(self, topology):
        """The child device in the collection must not be a standalone node."""
        assert len(topology["nodes"]) == 3
        assert "DEADBEEF00000001" not in topology["nodes"]

    def test_total_devices(self, topology):
        # 3 routers + 4 children = 7
        assert topology["total_devices"] == 7

    def test_leader_role_identified(self, topology):
        leader = topology["nodes"]["1EA5312CFB153F0B"]
        assert leader["role"] == "leader"
        assert leader["leader_cost"] == 0

    def test_router_role_identified(self, topology):
        assert topology["nodes"]["96308C2577D6EA17"]["role"] == "router"
        assert topology["nodes"]["A4B3C2D1E0F09876"]["role"] == "router"

    def test_link_quality_from_connectivity(self, topology):
        for node in topology["nodes"].values():
            assert node["link_quality"] == 3

    def test_child_counts(self, topology):
        counts = {addr: node["child_count"] for addr, node in topology["nodes"].items()}
        assert counts["1EA5312CFB153F0B"] == 1
        assert counts["96308C2577D6EA17"] == 1
        assert counts["A4B3C2D1E0F09876"] == 2

    def test_sleepy_vs_active_children(self, topology):
        node = topology["nodes"]["A4B3C2D1E0F09876"]
        types = sorted(child["type"] for child in node["children"])
        # rxOnWhenIdle False -> sleepy, True -> active
        assert types == ["active", "sleepy"]

    def test_connections_from_route64(self, topology):
        leader = topology["nodes"]["1EA5312CFB153F0B"]
        assert leader["connections"]
        assert leader["connections"][0]["router_id"] == 2
        assert leader["connections"][0]["cost"] == 1

    def test_matter_split(self, topology):
        assert len(topology["matter_devices"]["thread"]) == 3
        assert len(topology["matter_devices"]["wifi"]) == 2
        assert topology["matter_devices"]["total"] == 5

    def test_svg_generation(self, topology):
        coordinator = _build_coordinator()
        svg = coordinator.generate_svg(topology)
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
        assert "MyHome1038137341" in svg


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
