"""The Redline CNC (RTS-X) integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant

from .const import (
    CONF_ENABLE_GCODE_IO,
    DEFAULT_ENABLE_GCODE_IO,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import RedlineCncCoordinator

RedlineCncConfigEntry = ConfigEntry[RedlineCncCoordinator]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
) -> bool:
    """Set up Redline CNC from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
    name: str = entry.data.get(CONF_NAME, DEFAULT_NAME)
    enable_gcode_io: bool = entry.options.get(
        CONF_ENABLE_GCODE_IO,
        entry.data.get(CONF_ENABLE_GCODE_IO, DEFAULT_ENABLE_GCODE_IO),
    )

    coordinator = RedlineCncCoordinator(
        hass=hass,
        host=host,
        port=port,
        name=name,
        enable_gcode_io=enable_gcode_io,
    )
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await coordinator.async_start()

    async def _async_on_hass_stop(_event: Event) -> None:
        await coordinator.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_hass_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: RedlineCncCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_stop()
    return unload_ok
