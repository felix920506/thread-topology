"""Thread Network Topology integration for Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DEFAULT_OTBR_URL
from .coordinator import ThreadTopologyCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# Frontend custom card shipped with the integration. It is served from a static
# path and registered as an ES-module resource so the user does not have to add
# it under Settings > Dashboards > Resources manually.
FRONTEND_URL_BASE = "/thread_topology"
CARD_FILENAME = "thread-topology-card.js"
_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"
# Bump when the card JS changes so browsers re-fetch it (cache-buster).
CARD_VERSION = "0.7.2-vis2"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and register the custom Lovelace card (once per HA run)."""
    if hass.data.get(_FRONTEND_REGISTERED):
        return
    hass.data[_FRONTEND_REGISTERED] = True

    www_dir = Path(__file__).parent / "www"
    card_url = f"{FRONTEND_URL_BASE}/{CARD_FILENAME}"

    # The card is cosmetic: never let a frontend hiccup block the integration's
    # sensors from loading. The whole www/ directory is served so the card and
    # its bundled vis-network library are both reachable under the same base.
    try:
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(FRONTEND_URL_BASE, str(www_dir), False)]
            )
        except ImportError:
            # Older HA without StaticPathConfig: fall back to the sync registrar.
            hass.http.register_static_path(FRONTEND_URL_BASE, str(www_dir), False)

        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{card_url}?v={CARD_VERSION}")
    except Exception:  # noqa: BLE001 - frontend may be unavailable in some setups
        _LOGGER.warning(
            "Could not auto-register the Thread Topology card; add %s as a "
            "dashboard resource manually if you want the graph card",
            card_url,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Thread Topology from a config entry."""
    await _async_register_frontend(hass)

    otbr_url = entry.data.get("otbr_url", DEFAULT_OTBR_URL)

    coordinator = ThreadTopologyCoordinator(hass, otbr_url)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: ThreadTopologyCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
