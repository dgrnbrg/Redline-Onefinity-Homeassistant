"""Button platform for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RedlineCncConfigEntry
from .coordinator import RedlineCncCoordinator
from .entity import RedlineCncEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Redline CNC buttons based on a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([RedlineCncRefreshButton(coordinator, entry.entry_id)])


class RedlineCncRefreshButton(RedlineCncEntity, ButtonEntity):
    """Button to trigger an immediate status/file/tool refresh over WebSocket."""

    _attr_name = "Refresh Status"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator, entry_id, "refresh_status")

    @property
    def available(self) -> bool:
        """Return True if connected."""
        return self.coordinator.data.connected

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_request_refresh_all()
