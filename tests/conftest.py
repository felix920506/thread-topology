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
    """Return a mock OTBR ``GET /api/node`` response (JSON:API, camelCase).

    Mirrors a real response: the queried node is a *router* (not the leader);
    ``rloc16`` is a hex string and the leader is identified via
    ``leaderData.leaderRouterId``.
    """
    return {
        "data": {
            "type": "threadBorderRouter",
            "id": "7690F04AB3B4E9DA",
            "attributes": {
                "extAddress": "7690F04AB3B4E9DA",
                "role": "router",
                "state": "router",
                "routerId": 15,
                "rloc16": "0x3c00",
                "routerCount": 3,
                "networkName": "MyHome1038137341",
                "leaderData": {
                    "partitionId": 1425094364,
                    "weighting": 64,
                    "dataVersion": 66,
                    "stableDataVersion": 136,
                    "leaderRouterId": 7,
                },
                "extPanId": "F56C3C34E80C9EA2",
            },
        }
    }


@pytest.fixture
def mock_otbr_diagnostics_response() -> dict:
    """Return a mock OTBR ``GET /api/diagnostics`` collection (JSON:API).

    Three routers; router id 7 is the leader (matches leaderRouterId). The route
    table TLV is named ``route`` and ``rloc16`` is a hex string, as on the live
    API. Children come from each router's ``childTable``.
    """
    return {
        "data": [
            {
                "type": "networkDiagnostics",
                "id": "diag-1",
                "attributes": {
                    "extAddress": "228942D83C99F228",
                    "rloc16": "0x1c00",
                    "routerId": 7,
                    "route": {
                        "routeData": [
                            {"routeId": 7, "linkQualityIn": 0, "linkQualityOut": 0, "routeCost": 1},
                            {"routeId": 15, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                            {"routeId": 61, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                        ]
                    },
                    "childTable": [
                        {"childId": 6, "timeout": 12, "linkQuality": 3,
                         "mode": {"rxOnWhenIdle": False, "deviceTypeFTD": False}},
                        {"childId": 15, "timeout": 12, "linkQuality": 2,
                         "mode": {"rxOnWhenIdle": True, "deviceTypeFTD": False}},
                    ],
                    # Richer "children" TLV: each child carries its extAddress
                    "children": [
                        {"childId": 6, "rloc16": "0x1c06", "timeout": 12,
                         "rxOnWhenIdle": False, "extAddress": "AAAA000000000006"},
                        {"childId": 15, "rloc16": "0x1c0f", "timeout": 12,
                         "rxOnWhenIdle": True, "extAddress": "BBBB00000000000F"},
                    ],
                },
            },
            {
                "type": "networkDiagnostics",
                "id": "diag-2",
                "attributes": {
                    "extAddress": "7690F04AB3B4E9DA",
                    "rloc16": "0x3c00",
                    "routerId": 15,
                    "isBorderRouter": True,
                    "route": {
                        "routeData": [
                            {"routeId": 7, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                            {"routeId": 15, "linkQualityIn": 0, "linkQualityOut": 0, "routeCost": 1},
                            {"routeId": 61, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                        ]
                    },
                    "childTable": [
                        {"childId": 1, "timeout": 12, "linkQuality": 3,
                         "mode": {"rxOnWhenIdle": False, "deviceTypeFTD": False}},
                    ],
                },
            },
            {
                "type": "networkDiagnostics",
                "id": "diag-3",
                "attributes": {
                    "extAddress": "4E6BC0581D23D773",
                    "rloc16": "0xf400",
                    "routerId": 61,
                    "isBorderRouter": True,
                    "vendorName": "Home Assistant",
                    "vendorModel": "OpenThread Border Router",
                    "route": {
                        "routeData": [
                            {"routeId": 7, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                            {"routeId": 15, "linkQualityIn": 3, "linkQualityOut": 3, "routeCost": 1},
                            {"routeId": 61, "linkQualityIn": 0, "linkQualityOut": 0, "routeCost": 1},
                        ]
                    },
                    "childTable": [
                        {"childId": 2, "timeout": 12, "linkQuality": 3,
                         "mode": {"rxOnWhenIdle": False, "deviceTypeFTD": False}},
                    ],
                },
            },
        ],
        "meta": {"collection": {"offset": 0, "limit": 200, "total": 3}},
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
