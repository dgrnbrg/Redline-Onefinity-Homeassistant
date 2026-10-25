"""Binary sensor platform for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RedlineCncConfigEntry
from .coordinator import RedlineCncCoordinator, RedlineCncData
from .entity import RedlineCncEntity


@dataclass(frozen=True)
class RedlineCncBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Redline CNC binary sensor entity."""

    is_on_fn: Callable[[RedlineCncData], bool] = lambda _: False
    attributes_fn: Callable[[RedlineCncData], dict[str, Any]] | None = None


BINARY_SENSOR_DESCRIPTIONS: tuple[RedlineCncBinarySensorEntityDescription, ...] = (
    RedlineCncBinarySensorEntityDescription(
        key="connected",
        name="Controller Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda d: d.connected,
    ),
    RedlineCncBinarySensorEntityDescription(
        key="job_running",
        name="Job Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:play-circle-outline",
        is_on_fn=lambda d: d.is_running,
        attributes_fn=lambda d: {
            "mach_state": d.mach_state,
            "file_name": d.file_name,
            "progress_percent": d.progress_percent,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="estop_active",
        name="E-Stop Active",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-octagon",
        is_on_fn=lambda d: d.estop_active,
        attributes_fn=lambda d: {"mach_state": d.mach_state},
    ),
    RedlineCncBinarySensorEntityDescription(
        key="tool_change_active",
        name="Tool Change Active",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:wrench-clock",
        is_on_fn=lambda d: d.tool_change_active,
        attributes_fn=lambda d: {
            "mach_state": d.mach_state,
            "active_tool": d.active_tool,
            "current_line": d.current_line_number,
            "file_name": d.file_name,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_spindle",
        name="IO Spindle",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:saw-blade",
        is_on_fn=lambda d: d.spindle_on,
        attributes_fn=lambda d: {
            "target_rpm": d.spindle_rpm,
            "direction": "CW (M3)" if d.spindle_cw else "CCW (M4)",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_vacuum",
        name="IO Vacuum / Flood Coolant (M8)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:robot-vacuum",
        is_on_fn=lambda d: d.vacuum_on,
        attributes_fn=lambda d: {
            "gcode": "M8 / M9",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_air_assist",
        name="IO Air Assist / Mist Coolant (M7)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:weather-windy",
        is_on_fn=lambda d: d.air_assist_on,
        attributes_fn=lambda d: {
            "gcode": "M7 / M9",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="controller_patch_installed",
        name="Controller Patch Installed",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:puzzle-check-outline",
        is_on_fn=lambda d: d.patch_installed,
        attributes_fn=lambda d: {
            "patch_version": d.patch_version,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="spindle_warmup_active",
        name="Spindle Warmup Active",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:fire",
        is_on_fn=lambda d: d.spindle_warmup_active,
        attributes_fn=lambda d: {
            "stage": d.spindle_warmup_stage,
            "remaining_seconds": d.spindle_warmup_remaining_seconds,
            "status": d.spindle_warmup_status,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="macro_running",
        name="Macro Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:script-play-outline",
        is_on_fn=lambda d: d.macro_running,
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_aux_0",
        name="IO Aux Output P0 (OUT0)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:electric-switch",
        is_on_fn=lambda d: d.aux_outputs.get(0, False),
        attributes_fn=lambda d: {
            "pin": "P0",
            "label": d.output_labels.get(0, "OUT0"),
            "gcode": "M62/M64 P0 (on), M63/M65 P0 (off)",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_aux_1",
        name="IO Aux Output P1 (OUT1)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:electric-switch",
        is_on_fn=lambda d: d.aux_outputs.get(1, False),
        attributes_fn=lambda d: {
            "pin": "P1",
            "label": d.output_labels.get(1, "OUT1"),
            "gcode": "M62/M64 P1 (on), M63/M65 P1 (off)",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_aux_2",
        name="IO Aux Output P2 (OUT2)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:electric-switch",
        is_on_fn=lambda d: d.aux_outputs.get(2, False),
        attributes_fn=lambda d: {
            "pin": "P2",
            "label": d.output_labels.get(2, "OUT2"),
            "gcode": "M62/M64 P2 (on), M63/M65 P2 (off)",
            "source": d.io_source,
        },
    ),
    RedlineCncBinarySensorEntityDescription(
        key="io_aux_3",
        name="IO Aux Output P3 (OUT3)",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:electric-switch",
        is_on_fn=lambda d: d.aux_outputs.get(3, False),
        attributes_fn=lambda d: {
            "pin": "P3",
            "label": d.output_labels.get(3, "OUT3"),
            "gcode": "M62/M64 P3 (on), M63/M65 P3 (off)",
            "source": d.io_source,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Redline CNC binary sensors based on a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        RedlineCncBinarySensor(coordinator, entry.entry_id, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class RedlineCncBinarySensor(RedlineCncEntity, BinarySensorEntity):
    """Representation of a Redline CNC Binary Sensor."""

    entity_description: RedlineCncBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
        description: RedlineCncBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if self.entity_description.key == "connected":
            return True
        return self.coordinator.data.connected

    @property
    def is_on(self) -> bool:
        """Return True if the binary sensor is on."""
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator.data)
        return None
