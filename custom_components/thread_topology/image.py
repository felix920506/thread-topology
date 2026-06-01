"""Image platform for Thread Topology.

Exposes the generated topology diagram as an image entity so it can be added to
a dashboard directly (as an entity, or via a Picture/Image card) without writing
any template or referencing the SVG file by URL.
"""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ThreadTopologyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Thread Topology image entity."""
    coordinator: ThreadTopologyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ThreadTopologyImage(coordinator, entry)])


class ThreadTopologyImage(CoordinatorEntity[ThreadTopologyCoordinator], ImageEntity):
    """An image entity rendering the Thread network topology diagram (SVG)."""

    _attr_has_entity_name = True
    _attr_name = "Thread Topology"
    _attr_icon = "mdi:family-tree"
    _attr_content_type = "image/svg+xml"

    def __init__(
        self,
        coordinator: ThreadTopologyCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the image entity."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{entry.entry_id}_topology_image"
        self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the image timestamp so the frontend reloads the diagram."""
        self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the current topology diagram as SVG bytes."""
        if not self.coordinator.data:
            return None
        return self.coordinator.generate_svg(self.coordinator.data).encode("utf-8")
