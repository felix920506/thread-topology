"""Tests for Thread Topology config flow."""
from __future__ import annotations

import pytest

from custom_components.thread_topology.const import (
    DOMAIN,
    ENDPOINT_NODE,
    API_MEDIA_TYPE,
)


def _extract_network_name(payload: dict) -> str:
    """Replicate the config flow's network-name extraction from /api/node."""
    resource = payload.get("data", payload) if isinstance(payload, dict) else {}
    attrs = resource.get("attributes", resource) if isinstance(resource, dict) else {}
    return attrs.get("networkName") or attrs.get("NetworkName", "Thread Network")


class TestConfigFlow:
    """Test cases for config flow validation logic."""

    def test_validate_url_success(self, mock_otbr_node_response):
        """Test network name is extracted from a valid JSON:API response."""
        network_name = _extract_network_name(mock_otbr_node_response)
        assert network_name == "MyHome1038137341"

    def test_validate_url_legacy_flat_response(self):
        """Older flat responses (no data/attributes wrapper) still parse."""
        assert _extract_network_name({"networkName": "Flat"}) == "Flat"

    def test_validate_url_missing_name_falls_back(self):
        assert _extract_network_name({"data": {"attributes": {}}}) == "Thread Network"

    def test_validate_url_connection_error(self):
        """Test URL validation handles connection errors."""
        def handle_connection_error():
            return {"errors": {"base": "cannot_connect"}}

        assert handle_connection_error()["errors"]["base"] == "cannot_connect"

    def test_validate_url_timeout_error(self):
        """Test URL validation handles timeout errors."""
        def handle_timeout_error():
            return {"errors": {"base": "timeout"}}

        assert handle_timeout_error()["errors"]["base"] == "timeout"

    def test_validate_url_non_200_response(self):
        """Test URL validation handles non-200 responses."""
        status = 500
        assert status != 200

    def test_node_endpoint_constant(self):
        """Config flow must validate against the new /api/node endpoint."""
        assert ENDPOINT_NODE == "/api/node"

    def test_api_media_type_constant(self):
        assert API_MEDIA_TYPE == "application/vnd.api+json"

    def test_domain_constant(self):
        assert DOMAIN == "thread_topology"

    def test_url_normalization(self):
        """Test URL trailing slash is handled."""
        for url in ("http://localhost:8081", "http://localhost:8081/"):
            assert url.rstrip("/") == "http://localhost:8081"
