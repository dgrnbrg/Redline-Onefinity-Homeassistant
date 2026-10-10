"""Config flow for Redline CNC (RTS-X) integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ENABLE_GCODE_IO,
    DEFAULT_ENABLE_GCODE_IO,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
)
from .coordinator import async_probe_controller

_LOGGER = logging.getLogger(__name__)


def _normalize_host(raw_host: str) -> str:
    """Normalize a host or URL string (e.g. 'http://192.168.60.63') into a hostname/IP."""
    cleaned = raw_host.strip()
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        if parsed.hostname:
            return parsed.hostname
    return cleaned.split("/")[0].split(":")[0]


class RedlineCncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Redline CNC (RTS-X)."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = _normalize_host(user_input[CONF_HOST])
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            name = user_input.get(CONF_NAME, DEFAULT_NAME)
            enable_gcode_io = bool(
                user_input.get(CONF_ENABLE_GCODE_IO, DEFAULT_ENABLE_GCODE_IO)
            )

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            try:
                await async_probe_controller(session, host, port)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unable to connect to Redline CNC at ws://%s:%s: %s",
                    host,
                    port,
                    err,
                )
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"{name} ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_NAME: name,
                        CONF_ENABLE_GCODE_IO: enable_gcode_io,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="192.168.60.63"): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Optional(
                    CONF_ENABLE_GCODE_IO,
                    default=DEFAULT_ENABLE_GCODE_IO,
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> RedlineCncOptionsFlowHandler:
        """Get the options flow for this handler."""
        return RedlineCncOptionsFlowHandler(config_entry)


class RedlineCncOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Redline CNC."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_gcode_io = self._config_entry.options.get(
            CONF_ENABLE_GCODE_IO,
            self._config_entry.data.get(
                CONF_ENABLE_GCODE_IO,
                DEFAULT_ENABLE_GCODE_IO,
            ),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_GCODE_IO,
                        default=current_gcode_io,
                    ): bool,
                }
            ),
        )
