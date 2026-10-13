"""Sensor platform for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, REVOLUTIONS_PER_MINUTE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RedlineCncConfigEntry
from .const import AXIS_NAMES, MACHINE_STATE_OPTIONS
from .coordinator import RedlineCncCoordinator, RedlineCncData
from .entity import RedlineCncEntity


@dataclass(frozen=True)
class RedlineCncSensorEntityDescription(SensorEntityDescription):
    """Describe a Redline CNC sensor entity."""

    value_fn: Callable[[RedlineCncData], Any] = lambda _: None
    unit_fn: Callable[[RedlineCncData], str | None] | None = None
    attributes_fn: Callable[[RedlineCncData], dict[str, Any]] | None = None


def _format_mach_state(data: RedlineCncData) -> str:
    """Format machine state matching MACHINE_STATE_OPTIONS."""
    if not data.connected:
        return "Disconnected"
    raw = (data.mach_state or "").strip()
    if not raw:
        return "Unknown"
    lower = raw.lower()
    for opt in MACHINE_STATE_OPTIONS:
        if opt.lower() == lower:
            return opt
    if "estop" in lower or "e-stop" in lower:
        return "E-Stop"
    if "prob" in lower:
        return "Probing"
    if "hom" in lower:
        return "Homing"
    if "jog" in lower:
        return "Jogging"
    if "mov" in lower:
        return "Moving"
    if "run" in lower or "cut" in lower:
        return "Running"
    if "read" in lower or "idle" in lower:
        return "Ready"
    if "paus" in lower or "hold" in lower:
        return "Paused"
    return "Unknown"


def _format_duration(seconds: int) -> str:
    """Format seconds into HH:MM:SS."""
    if seconds <= 0:
        return "00:00:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _active_tool_label(data: RedlineCncData) -> str | None:
    """Return active tool label or default first tool if available."""
    tool_num = data.active_tool
    if tool_num is not None:
        for tool in data.tools:
            if tool.get("tool_number") == tool_num:
                desc = tool.get("description") or ""
                return f"T{tool_num} ({desc})" if desc else f"T{tool_num}"
        return f"T{tool_num}"
    if data.tools:
        first = data.tools[0]
        tnum = first.get("tool_number", 1)
        desc = first.get("description") or ""
        return f"T{tnum} ({desc})" if desc else f"T{tnum}"
    return None


SENSOR_DESCRIPTIONS: tuple[RedlineCncSensorEntityDescription, ...] = (
    RedlineCncSensorEntityDescription(
        key="mach_state",
        name="Machine State",
        icon="mdi:state-machine",
        device_class=SensorDeviceClass.ENUM,
        options=list(MACHINE_STATE_OPTIONS),
        value_fn=_format_mach_state,
        attributes_fn=lambda d: {
            "machine_states": list(MACHINE_STATE_OPTIONS),
            "is_running": d.is_running,
            "estop_active": d.estop_active,
            "units": d.units,
            "raw_state": d.mach_state,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="progress",
        name="Job Progress",
        icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.progress_percent,
        attributes_fn=lambda d: {
            "raw_progress": d.progress,
            "current_line": d.current_line_number,
            "total_lines": d.total_lines,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="current_file",
        name="Current File",
        icon="mdi:file-code-outline",
        value_fn=lambda d: d.file_name or "None",
        attributes_fn=lambda d: {
            "file_info": d.file_info,
            "full_path": d.file_paths.get(
                d.file_name, f"gcode/{d.file_name}" if d.file_name else ""
            ),
            "total_files_on_controller": d.file_count,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="current_line",
        name="Current Line",
        icon="mdi:format-list-numbered",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current_line_number,
    ),
    RedlineCncSensorEntityDescription(
        key="total_lines",
        name="Total Lines",
        icon="mdi:format-list-bulleted-type",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.total_lines,
    ),
    RedlineCncSensorEntityDescription(
        key="eta",
        name="ETA",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.eta,
        attributes_fn=lambda d: {
            "remaining_time": _format_duration(d.remaining_seconds),
            "elapsed_time": _format_duration(d.elapsed_seconds),
            "raw_elapsed_eta": d.elapsed_eta,
            "job_start_time": d.job_start_utc.isoformat() if d.job_start_utc else None,
            "is_running": d.is_running,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="spindle_rpm",
        name="Target Spindle Speed",
        icon="mdi:fan",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.spindle_rpm,
        attributes_fn=lambda d: {
            "spindle_on": d.spindle_on,
            "direction": "CW (M3)" if d.spindle_cw else "CCW (M4)",
            "source": d.io_source,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="feed_rate",
        name="Target Feed Rate",
        icon="mdi:speedometer",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.feed_rate,
        unit_fn=lambda d: "in/min" if d.units == "imperial" else "mm/min",
    ),
    RedlineCncSensorEntityDescription(
        key="active_tool",
        name="Active Tool",
        icon="mdi:tools",
        value_fn=_active_tool_label,
        attributes_fn=lambda d: {
            "tool_number": d.active_tool,
            "tool_table": d.tools,
        },
    ),
    RedlineCncSensorEntityDescription(
        key="units",
        name="Coordinate Units",
        icon="mdi:ruler",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.units,
    ),
    RedlineCncSensorEntityDescription(
        key="firmware_version",
        name="RTS-X Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.version,
    ),
    *(
        RedlineCncSensorEntityDescription(
            key=f"work_pos_{axis.lower()}",
            name=f"Work Position {axis}",
            icon=f"mdi:axis-{axis.lower()}-arrow"
            if axis in ("X", "Y", "Z")
            else "mdi:rotate-orbit",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            entity_registry_enabled_default=False,
            value_fn=lambda d, idx=i: d.work_pos[idx],
            unit_fn=lambda d: d.unit_symbol,
            attributes_fn=lambda d, idx=i: {
                "machine_pos": d.mach_pos[idx],
                "wcs_offset": d.wcs_offset[idx],
                "units": d.units,
            },
        )
        for i, axis in enumerate(AXIS_NAMES)
    ),
    *(
        RedlineCncSensorEntityDescription(
            key=f"mach_pos_{axis.lower()}",
            name=f"Machine Position {axis}",
            icon=f"mdi:axis-{axis.lower()}-arrow"
            if axis in ("X", "Y", "Z")
            else "mdi:rotate-orbit",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda d, idx=i: d.mach_pos[idx],
            unit_fn=lambda d: d.unit_symbol,
        )
        for i, axis in enumerate(AXIS_NAMES)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Redline CNC sensors based on a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        RedlineCncSensor(coordinator, entry.entry_id, description)
        for description in SENSOR_DESCRIPTIONS
    )


class RedlineCncSensor(RedlineCncEntity, SensorEntity):
    """Representation of a Redline CNC Sensor."""

    entity_description: RedlineCncSensorEntityDescription

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
        description: RedlineCncSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return True if controller is connected (or always for mach_state)."""
        if self.entity_description.key == "mach_state":
            return True
        return self.coordinator.data.connected

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the dynamic or static unit of measurement."""
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self.coordinator.data)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator.data)
        return None
