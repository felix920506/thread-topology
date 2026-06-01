"""Fixtures for Thread Topology tests."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock homeassistant modules so the integration can be imported without HA
# installed. Done in conftest so it applies before any test module is collected.
for _mod in (
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.update_coordinator",
):
    sys.modules.setdefault(_mod, MagicMock())


# DataUpdateCoordinator is subclassed by the integration, so it must be a real
# (subscriptable) class rather than a MagicMock. UpdateFailed must be a real
# exception so `raise ... from err` and `except UpdateFailed` work.
class _FakeDataUpdateCoordinator:
    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


class _FakeUpdateFailed(Exception):
    pass


_uc = sys.modules["homeassistant.helpers.update_coordinator"]
_uc.DataUpdateCoordinator = _FakeDataUpdateCoordinator
_uc.UpdateFailed = _FakeUpdateFailed


@pytest.fixture
def mock_otbr_node_response() -> dict:
    """Return a mock OTBR ``GET /api/node`` response (JSON:API, camelCase)."""
    return {
        "data": {
            "type": "threadBorderRouter",
            "id": "1EA5312CFB153F0B",
            "attributes": {
                "baId": "175B0E832E7217C5C5A630B547C044E4",
                "state": "leader",
                "numOfRouter": 3,
                "rlocAddress": "fd2a:398d:f276:6b9c:0:ff:fe00:d800",
                "extAddress": "1EA5312CFB153F0B",
                "networkName": "MyHome1038137341",
                "rloc16": 55296,
                "leaderData": {
                    "partitionId": 1055464771,
                    "weighting": 64,
                    "dataVersion": 126,
                    "stableDataVersion": 159,
                    "leaderRouterId": 54,
                },
                "extPanId": "78ACC8F0AE5249C5",
            },
        }
    }


@pytest.fixture
def mock_otbr_devices_response() -> dict:
    """Return a mock OTBR ``GET /api/devices`` collection (JSON:API)."""
    return {
        "data": [
            {
                "type": "threadBorderRouter",
                "id": "1EA5312CFB153F0B",
                "attributes": {
                    "extAddress": "1EA5312CFB153F0B",
                    "rloc16": 55296,
                    "role": "leader",
                },
            },
            {
                "type": "threadDevice",
                "id": "96308C2577D6EA17",
                "attributes": {
                    "extAddress": "96308C2577D6EA17",
                    "rloc16": 8192,
                    "role": "router",
                },
            },
            {
                "type": "threadDevice",
                "id": "A4B3C2D1E0F09876",
                "attributes": {
                    "extAddress": "A4B3C2D1E0F09876",
                    "rloc16": 16384,
                    "role": "router",
                },
            },
            {
                # A child device — must NOT become a standalone node.
                "type": "threadDevice",
                "id": "DEADBEEF00000001",
                "attributes": {
                    "extAddress": "DEADBEEF00000001",
                    "rloc16": 8216,
                    "role": "child",
                },
            },
        ]
    }


@pytest.fixture
def mock_diagnostics_by_addr() -> dict:
    """Return mock per-router diagnostics keyed by normalized extended address.

    This mirrors what ``_fetch_diagnostics`` produces after running
    ``getNetworkDiagnosticTask`` for each router and reading ``/api/diagnostics``.
    """
    return {
        "1EA5312CFB153F0B": {
            "extAddress": "1EA5312CFB153F0B",
            "rloc16": 55296,
            "mode": {"rxOnWhenIdle": True, "deviceType": True, "networkData": True},
            "connectivity": {
                "linkQuality3": 1,
                "linkQuality2": 0,
                "linkQuality1": 0,
                "leaderCost": 0,
            },
            "route64": {
                "routeData": [
                    {"routerId": 2, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                ]
            },
            "childTable": [
                {
                    "childId": 9,
                    "timeout": 12,
                    "rloc16": 55305,
                    "mode": {"rxOnWhenIdle": False, "deviceType": False, "networkData": False},
                },
            ],
            "ipv6AddressList": ["fd2a:398d:f276:6b9c:0:ff:fe00:d800"],
        },
        "96308C2577D6EA17": {
            "extAddress": "96308C2577D6EA17",
            "rloc16": 8192,
            "mode": {"rxOnWhenIdle": True, "deviceType": True, "networkData": True},
            "connectivity": {
                "linkQuality3": 1,
                "linkQuality2": 0,
                "linkQuality1": 0,
                "leaderCost": 1,
            },
            "route64": {
                "routeData": [
                    {"routerId": 13, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                ]
            },
            "childTable": [
                {
                    "childId": 24,
                    "timeout": 12,
                    "rloc16": 8216,
                    "mode": {"rxOnWhenIdle": False, "deviceType": False, "networkData": False},
                },
            ],
            "ipv6AddressList": ["fd2a:398d:f276:6b9c:0:ff:fe00:2000"],
        },
        "A4B3C2D1E0F09876": {
            "extAddress": "A4B3C2D1E0F09876",
            "rloc16": 16384,
            "mode": {"rxOnWhenIdle": True, "deviceType": True, "networkData": True},
            "connectivity": {
                "linkQuality3": 1,
                "linkQuality2": 0,
                "linkQuality1": 0,
                "leaderCost": 1,
            },
            "route64": {
                "routeData": [
                    {"routerId": 13, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                ]
            },
            "childTable": [
                {
                    "childId": 5,
                    "timeout": 12,
                    "rloc16": 16389,
                    "mode": {"rxOnWhenIdle": False, "deviceType": False, "networkData": False},
                },
                {
                    "childId": 8,
                    "timeout": 12,
                    "rloc16": 16392,
                    "mode": {"rxOnWhenIdle": True, "deviceType": False, "networkData": False},
                },
            ],
            "ipv6AddressList": ["fd2a:398d:f276:6b9c:0:ff:fe00:4000"],
        },
    }


@pytest.fixture
def mock_matter_devices() -> list:
    """Return mock Matter devices from device registry."""
    return [
        {
            "name": "Meross MS605",
            "model": "Smart Presence Sensor",
            "manufacturer": "Meross",
            "transport": "thread",
        },
        {
            "name": "Aqara Door Sensor P2",
            "model": "Aqara Door and Window Sensor P2",
            "manufacturer": "Aqara",
            "transport": "thread",
        },
        {
            "name": "Eve Motion",
            "model": "Eve Motion",
            "manufacturer": "Eve Systems",
            "transport": "thread",
        },
        {
            "name": "Nuki Smart Lock",
            "model": "Smart Lock",
            "manufacturer": "Nuki",
            "transport": "wifi",
        },
        {
            "name": "SONOFF Switch",
            "model": "WiFi Smart Switch",
            "manufacturer": "SONOFF",
            "transport": "wifi",
        },
    ]
