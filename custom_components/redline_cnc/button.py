"""Button platform for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from typing import Any

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
    entities: list[ButtonEntity] = [
        RedlineCncRefreshButton(coordinator, entry.entry_id),
        RedlineCncInstallPatchButton(coordinator, entry.entry_id),
        RedlineCncUninstallPatchButton(coordinator, entry.entry_id),
        RedlineCncStartSpindleWarmupButton(coordinator, entry.entry_id),
        RedlineCncCancelSpindleWarmupButton(coordinator, entry.entry_id),
        RedlineCncAbortMacroButton(coordinator, entry.entry_id),
    ]
    for macro_idx in range(12):
        entities.append(
            RedlineCncMacroButton(coordinator, entry.entry_id, macro_idx)
        )
    async_add_entities(entities)


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


class RedlineCncInstallPatchButton(RedlineCncEntity, ButtonEntity):
    """1-Click button to safely install/update the controller patch over SSH with Git backup."""

    _attr_name = "Install Controller Patch"
    _attr_icon = "mdi:package-down"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the install patch button."""
        super().__init__(coordinator, entry_id, "install_controller_patch")

    @property
    def available(self) -> bool:
        """Return True if connected to the controller."""
        return self.coordinator.data.connected

    async def async_press(self) -> None:
        """Install the controller patch via SSH."""
        await self.coordinator.async_install_controller_patch()


class RedlineCncUninstallPatchButton(RedlineCncEntity, ButtonEntity):
    """1-Click button to uninstall the controller patch and restore vendor Web_API.py."""

    _attr_name = "Uninstall Controller Patch"
    _attr_icon = "mdi:package-up"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the uninstall patch button."""
        super().__init__(coordinator, entry_id, "uninstall_controller_patch")

    @property
    def available(self) -> bool:
        """Return True if connected to the controller."""
        return self.coordinator.data.connected

    async def async_press(self) -> None:
        """Uninstall the controller patch and restore original Web_API.py via SSH."""
        await self.coordinator.async_uninstall_controller_patch()


class RedlineCncStartSpindleWarmupButton(RedlineCncEntity, ButtonEntity):
    """Button to launch the native 5-stage Spindle Warmup routine on the controller."""

    _attr_name = "Start Spindle Warmup"
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the start spindle warmup button."""
        super().__init__(coordinator, entry_id, "start_spindle_warmup")

    @property
    def available(self) -> bool:
        """Return True if connected, patch installed, and machine is idle."""
        data = self.coordinator.data
        return (
            data.connected
            and data.patch_installed
            and not data.is_running
            and not data.estop_active
        )

    async def async_press(self) -> None:
        """Start the spindle warmup sequence."""
        await self.coordinator.async_start_spindle_warmup()


class RedlineCncCancelSpindleWarmupButton(RedlineCncEntity, ButtonEntity):
    """Button to cancel an active Spindle Warmup routine and stop the spindle."""

    _attr_name = "Cancel Spindle Warmup"
    _attr_icon = "mdi:fire-off"

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the cancel spindle warmup button."""
        super().__init__(coordinator, entry_id, "cancel_spindle_warmup")

    @property
    def available(self) -> bool:
        """Return True if connected and patch installed."""
        data = self.coordinator.data
        return data.connected and data.patch_installed

    async def async_press(self) -> None:
        """Cancel the spindle warmup sequence."""
        await self.coordinator.async_cancel_spindle_warmup()


class RedlineCncAbortMacroButton(RedlineCncEntity, ButtonEntity):
    """Button to abort an actively running macro on the controller."""

    _attr_name = "Abort Macro"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the abort macro button."""
        super().__init__(coordinator, entry_id, "abort_macro")

    @property
    def available(self) -> bool:
        """Return True if connected and patch installed."""
        data = self.coordinator.data
        return data.connected and data.patch_installed

    async def async_press(self) -> None:
        """Abort the currently running macro."""
        await self.coordinator.async_abort_macro()


class RedlineCncMacroButton(RedlineCncEntity, ButtonEntity):
    """Button to trigger one of the 12 macro slots on the Redline CNC controller."""

    _attr_icon = "mdi:script-text-play-outline"

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
        macro_index: int,
    ) -> None:
        """Initialize a macro slot button (0..11)."""
        super().__init__(coordinator, entry_id, f"macro_{macro_index + 1}")
        self._macro_index = macro_index

    def _macro_info(self) -> dict[str, Any] | None:
        """Return the macro dict for this slot if reported by the controller."""
        for m in self.coordinator.data.macros:
            if m.get("index") == self._macro_index:
                return m
        return None

    @property
    def name(self) -> str:
        """Return the button name including the configured macro title if present."""
        slot_num = self._macro_index + 1
        info = self._macro_info()
        macro_name = str(info.get("name", "")).strip() if info else ""
        if macro_name:
            return f"Macro {slot_num}: {macro_name}"
        return f"Macro {slot_num}"

    @property
    def available(self) -> bool:
        """Return True when connected, patched, idle, and the macro slot is configured."""
        data = self.coordinator.data
        if not (data.connected and data.patch_installed):
            return False
        if data.is_running or data.estop_active or data.macro_running:
            return False
        info = self._macro_info()
        if info is None:
            return False
        return bool(str(info.get("name", "")).strip() and str(info.get("path", "")).strip())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return macro slot metadata."""
        info = self._macro_info() or {}
        name = str(info.get("name", "")).strip()
        path = str(info.get("path", "")).strip()
        return {
            "slot": self._macro_index + 1,
            "index": self._macro_index,
            "macro_name": name,
            "file_path": path,
            "configured": bool(name and path),
        }

    async def async_press(self) -> None:
        """Execute this macro slot on the controller."""
        await self.coordinator.async_run_macro(self._macro_index)
