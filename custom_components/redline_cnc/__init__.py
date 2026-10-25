"""The Redline CNC (RTS-X) integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_ENABLE_GCODE_IO,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USER,
    CONF_WEB_PORT,
    DEFAULT_ENABLE_GCODE_IO,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DEFAULT_WEB_PORT,
    DOMAIN,
    PLATFORMS,
    SERVICE_ABORT_MACRO,
    SERVICE_CANCEL_SPINDLE_WARMUP,
    SERVICE_INSTALL_PATCH,
    SERVICE_RUN_MACRO,
    SERVICE_SET_OUTPUT,
    SERVICE_START_SPINDLE_WARMUP,
    SERVICE_UNINSTALL_PATCH,
    SERVICE_RESTART_RTS1,
)
from .coordinator import RedlineCncCoordinator

RedlineCncConfigEntry = ConfigEntry[RedlineCncCoordinator]


def _resolve_coordinator(
    hass: HomeAssistant,
    call: ServiceCall,
) -> RedlineCncCoordinator:
    """Resolve the target RedlineCncCoordinator for a service call."""
    coordinators: dict[str, RedlineCncCoordinator] = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("No Redline CNC controller is configured")
    entry_id = call.data.get("entry_id")
    if isinstance(entry_id, str) and entry_id in coordinators:
        return coordinators[entry_id]
    return next(iter(coordinators.values()))


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Redline CNC domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_MACRO):
        return

    async def _handle_run_macro(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        macro = call.data.get("macro")
        if macro is None:
            macro = call.data.get("index", 0)
        await coord.async_run_macro(macro)

    async def _handle_abort_macro(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        await coord.async_abort_macro()

    async def _handle_set_output(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        pin = int(call.data["pin"])
        state = bool(call.data["state"])
        await coord.async_set_output(pin, state)

    async def _handle_start_warmup(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        await coord.async_start_spindle_warmup()

    async def _handle_cancel_warmup(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        await coord.async_cancel_spindle_warmup()

    async def _handle_install_patch(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        await coord.async_install_controller_patch()

    async def _handle_uninstall_patch(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        await coord.async_uninstall_controller_patch()

    async def _handle_restart_rts1(call: ServiceCall) -> None:
        coord = _resolve_coordinator(hass, call)
        force = bool(call.data.get("force", False))
        await coord.async_restart_rts1(force=force)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_MACRO,
        _handle_run_macro,
        schema=vol.Schema(
            {
                vol.Optional("macro"): vol.Any(int, str),
                vol.Optional("index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=11)),
                vol.Optional("entry_id"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ABORT_MACRO,
        _handle_abort_macro,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OUTPUT,
        _handle_set_output,
        schema=vol.Schema(
            {
                vol.Required("pin"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
                vol.Required("state"): bool,
                vol.Optional("entry_id"): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_SPINDLE_WARMUP,
        _handle_start_warmup,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_SPINDLE_WARMUP,
        _handle_cancel_warmup,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_INSTALL_PATCH,
        _handle_install_patch,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNINSTALL_PATCH,
        _handle_uninstall_patch,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTART_RTS1,
        _handle_restart_rts1,
        schema=vol.Schema(
            {
                vol.Optional("force", default=False): bool,
                vol.Optional("entry_id"): str,
            }
        ),
    )


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
    web_port: int = int(
        entry.options.get(
            CONF_WEB_PORT,
            entry.data.get(CONF_WEB_PORT, DEFAULT_WEB_PORT),
        )
    )
    ssh_port: int = int(
        entry.options.get(
            CONF_SSH_PORT,
            entry.data.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
        )
    )
    ssh_user: str = str(
        entry.options.get(
            CONF_SSH_USER,
            entry.data.get(CONF_SSH_USER, DEFAULT_SSH_USER),
        )
    )
    ssh_password: str = str(
        entry.options.get(
            CONF_SSH_PASSWORD,
            entry.data.get(CONF_SSH_PASSWORD, DEFAULT_SSH_PASSWORD),
        )
    )

    coordinator = RedlineCncCoordinator(
        hass=hass,
        host=host,
        port=port,
        name=name,
        enable_gcode_io=enable_gcode_io,
        web_port=web_port,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
    )
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _async_register_services(hass)
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
