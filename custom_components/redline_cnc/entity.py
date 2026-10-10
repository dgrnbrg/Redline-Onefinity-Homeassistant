"""Base entity for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RedlineCncCoordinator


class RedlineCncEntity(CoordinatorEntity[RedlineCncCoordinator]):
    """Base class for Redline CNC entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for this Redline CNC controller."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.host}:{self.coordinator.port}")},
            name=self.coordinator.name,
            manufacturer="Redline CNC / RealTime CNC",
            model="RTS-X Controller",
            sw_version=self.coordinator.data.version or "RTS-X",
            configuration_url=f"http://{self.coordinator.host}",
        )
