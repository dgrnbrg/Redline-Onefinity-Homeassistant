"""Switch platform for the Redline CNC (RTS-X) integration (Hardware Outputs OUT0-OUT3)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
    """Set up Redline CNC hardware output switches (OUT0-OUT3)."""
    coordinator = entry.runtime_data
    async_add_entities(
        RedlineCncOutputSwitch(coordinator, entry.entry_id, pin)
        for pin in (0, 1, 2, 3)
    )


class RedlineCncOutputSwitch(RedlineCncEntity, SwitchEntity):
    """Switch to control and monitor Redline CNC hardware auxiliary output OUT0..OUT3."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:electric-switch"

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
        pin: int,
    ) -> None:
        """Initialize the hardware output switch."""
        super().__init__(coordinator, entry_id, f"output_{pin}")
        self._pin = pin

    @property
    def name(self) -> str:
        """Return the display name including user-configured label if customized."""
        default_label = f"OUT{self._pin}"
        label = self.coordinator.data.output_labels.get(self._pin, default_label)
        if label and label.upper() != default_label:
            return f"Output {self._pin} ({label})"
        return f"Output {self._pin} ({default_label})"

    @property
    def available(self) -> bool:
        """Return True if the controller is connected and the HA extension patch is active."""
        return (
            self.coordinator.data.connected
            and self.coordinator.data.patch_installed
        )

    @property
    def is_on(self) -> bool:
        """Return True if the hardware output is currently active."""
        return bool(self.coordinator.data.aux_outputs.get(self._pin, False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return metadata about the hardware output pin."""
        default_label = f"OUT{self._pin}"
        return {
            "pin": self._pin,
            "label": self.coordinator.data.output_labels.get(self._pin, default_label),
            "gcode": f"M62/M64 P{self._pin} (on), M63/M65 P{self._pin} (off)",
            "source": self.coordinator.data.io_source,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the hardware output ON."""
        await self.coordinator.async_set_output(self._pin, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the hardware output OFF."""
        await self.coordinator.async_set_output(self._pin, False)
