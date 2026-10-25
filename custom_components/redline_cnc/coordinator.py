"""WebSocket client and DataUpdateCoordinator for Redline CNC (RTS-X)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    API_HA_MACRO_ABORT,
    API_HA_MACRO_RUN,
    API_HA_OUTPUT,
    API_HA_STATUS,
    API_HA_WARMUP_CANCEL,
    API_HA_WARMUP_START,
    CMD_DOWNLOAD_FILE,
    CMD_REFRESH_FILE_LIST,
    CMD_REFRESH_STATUS,
    CMD_REQUEST_TOOL_TABLE,
    CMD_REQUEST_VERSION,
    CONF_ALLOW_REMOTE_MOTION,
    DEFAULT_ALLOW_REMOTE_MOTION,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DEFAULT_WEB_PORT,
    DOMAIN,
    IDLE_MACHINE_STATES,
    RUNNING_MACHINE_STATES,
)
from .gcode_parser import GcodeModalState, GcodeProgramTimeline
from .patch_manager import (
    async_install_patch_ssh,
    async_restart_controller_rts1,
    async_uninstall_patch_ssh,
)

_LOGGER = logging.getLogger(__name__)

_FILE_INFO_LINES_RE = re.compile(r"(\d+)\s+lines", re.IGNORECASE)
_TIME_TOKEN_RE = re.compile(r"(\d+):(\d{2})(?::(\d{2}(?:\.\d+)?))?")
_HMS_UNIT_RE = re.compile(
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
    re.IGNORECASE,
)


@dataclass
class RedlineCncData:
    """Snapshot of the Redline CNC controller state."""

    connected: bool = False
    version: str | None = None
    mach_state: str = "Disconnected"
    is_running: bool = False
    estop_active: bool = False
    file_name: str = ""
    file_info: str = ""
    progress: float = 0.0
    progress_percent: float = 0.0
    current_line: str = ""
    current_line_number: int = 0
    total_lines: int = 0
    reported_line_total: int = 0
    elapsed_eta: str = "00:00:00"
    eta: datetime | None = None
    remaining_seconds: int = 0
    elapsed_seconds: int = 0
    job_start_utc: datetime | None = None
    units: str = "metric"
    unit_symbol: str = "mm"
    mach_pos: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    wcs_offset: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    work_pos: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    spindle_on: bool = False
    spindle_cw: bool = True
    spindle_rpm: float = 0.0
    feed_rate: float = 0.0
    active_tool: int | None = None
    vacuum_on: bool = False
    air_assist_on: bool = False
    tool_change_active: bool = False
    aux_outputs: dict[int, bool] = field(
        default_factory=lambda: {0: False, 1: False, 2: False, 3: False}
    )
    output_labels: dict[int, str] = field(
        default_factory=lambda: {0: "OUT0", 1: "OUT1", 2: "OUT2", 3: "OUT3"}
    )
    io_source: str = "gcode_modal"
    tools: list[dict[str, Any]] = field(default_factory=list)
    file_paths: dict[str, str] = field(default_factory=dict)
    file_count: int = 0
    camera_available: bool = False
    frame_width: int | None = None
    frame_height: int | None = None
    patch_installed: bool = False
    patch_version: str | None = None
    spindle_warmup_active: bool = False
    spindle_warmup_stage: int = 0
    spindle_warmup_remaining_seconds: int = 0
    spindle_warmup_status: str = "Idle"
    macros: list[dict[str, Any]] = field(default_factory=list)
    macro_running: bool = False


async def async_probe_controller(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Connect briefly to ws://host:port to verify connectivity and fetch version."""
    url = f"ws://{host}:{port}"
    version: str | None = None
    got_status = False

    async with asyncio.timeout(timeout):
        async with session.ws_connect(url, heartbeat=15.0) as ws:
            await ws.send_json({"command": CMD_REQUEST_VERSION})
            await ws.send_json({"command": CMD_REFRESH_STATUS})
            start = time.monotonic()
            while time.monotonic() - start < 3.0:
                msg = await ws.receive(timeout=1.5)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("command") == "versionInfo":
                        version = str(payload.get("version", ""))
                    elif "mach_state" in payload or "file_name" in payload:
                        got_status = True
                    if version is not None and got_status:
                        break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

    return {"version": version, "got_status": got_status}


class RedlineCncCoordinator(DataUpdateCoordinator[RedlineCncData]):
    """Manages the persistent WebSocket connection to the Redline CNC RTS-X server."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        name: str,
        enable_gcode_io: bool = True,
        web_port: int = DEFAULT_WEB_PORT,
        ssh_port: int = DEFAULT_SSH_PORT,
        ssh_user: str = DEFAULT_SSH_USER,
        ssh_password: str = DEFAULT_SSH_PASSWORD,
    ) -> None:
        """Initialize the Redline CNC coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=None,  # Push-based via WebSocket
        )
        self.host = host
        self.port = port
        self.web_port = web_port
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.enable_gcode_io = enable_gcode_io
        self.data = RedlineCncData()

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._ext_poll_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._patch_lock = asyncio.Lock()

        # Camera frame state (decoded on-demand to minimize CPU load)
        self._latest_frame_b64: str | None = None
        self._latest_frame_number: int | None = None
        self._cached_decoded_frame_number: int | None = None
        self._cached_decoded_bytes: bytes | None = None
        self._frame_event = asyncio.Event()
        self._last_frame_monotonic: float = 0.0

        # Active G-code file download & modal IO timeline cache
        self._gcode_timelines: dict[str, GcodeProgramTimeline] = {}
        self._download_buffers: dict[str, bytearray] = {}
        self._requested_downloads: set[str] = set()

        # Track which loaded file has its ETA locked in so ETA does not tick every second
        self._eta_locked_for_file: str | None = None
        self._last_eta_duration_s: int | None = None
        self._job_start_mono: float | None = None
        self._job_start_utc: datetime | None = None
        self._last_job_running: bool = False

        # Track idle line number vs active G-code line progression so HMI probing
        # never triggers G-code modal IOs from a pre-loaded file
        self._idle_line_number: int = 0
        self._job_line_advanced: bool = False

    @property
    def ws_url(self) -> str:
        """Return the WebSocket URL."""
        return f"ws://{self.host}:{self.port}"

    @property
    def web_base_url(self) -> str:
        """Return the base HTTP URL for the RTS-X FastAPI server."""
        return f"http://{self.host}:{self.web_port}"

    @property
    def last_frame_number(self) -> int | None:
        """Return the latest camera frame number."""
        return self._latest_frame_number

    def get_latest_camera_image(self) -> bytes | None:
        """Decode and return the most recent JPEG frame from the camera stream."""
        if not self._latest_frame_b64:
            return None
        if (
            self._cached_decoded_bytes is not None
            and self._cached_decoded_frame_number == self._latest_frame_number
        ):
            return self._cached_decoded_bytes
        try:
            decoded = base64.b64decode(self._latest_frame_b64)
            self._cached_decoded_bytes = decoded
            self._cached_decoded_frame_number = self._latest_frame_number
            return decoded
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to decode camera frame: %s", err)
            return None

    async def async_wait_for_next_frame(self, timeout: float = 2.0) -> bytes | None:
        """Wait for the next camera frame to arrive and return its JPEG bytes."""
        self._frame_event.clear()
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self.get_latest_camera_image()

    async def async_start(self) -> None:
        """Start the persistent WebSocket listener and HA extension polling tasks."""
        self._stop_event.clear()
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = self.hass.async_create_background_task(
                self._async_ws_loop(),
                name=f"{DOMAIN}_{self.host}_{self.port}_ws",
            )
        if self._ext_poll_task is None or self._ext_poll_task.done():
            self._ext_poll_task = self.hass.async_create_background_task(
                self._async_ha_ext_poll_loop(),
                name=f"{DOMAIN}_{self.host}_{self.web_port}_ha_ext",
            )

    async def async_stop(self) -> None:
        """Stop the WebSocket listener and polling tasks and close the connection."""
        self._stop_event.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        for task in (self._listen_task, self._ext_poll_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._listen_task = None
        self._ext_poll_task = None

    async def async_send_command(self, payload: dict[str, Any]) -> bool:
        """Send a JSON command over the WebSocket if connected."""
        if self._ws is None or self._ws.closed:
            return False
        try:
            await self._ws.send_json(payload)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error sending WebSocket command %s: %s", payload, err)
            return False

    async def async_request_refresh_all(self) -> None:
        """Request fresh status, version, tool table, file list, and HA extension state."""
        await self.async_send_command({"command": CMD_REFRESH_STATUS})
        await self.async_send_command({"command": CMD_REQUEST_VERSION})
        await self.async_send_command({"command": CMD_REQUEST_TOOL_TABLE})
        await self.async_send_command({"command": CMD_REFRESH_FILE_LIST})
        await self.async_poll_ha_extension()

    async def _async_ha_ext_poll_loop(self) -> None:
        """Periodically poll the /api/ha/status endpoint while connected."""
        while not self._stop_event.is_set():
            if self.data.connected:
                try:
                    await self.async_poll_ha_extension()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Error polling /api/ha/status: %s", err)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def async_poll_ha_extension(self) -> bool:
        """Query GET /api/ha/status on the controller and update extension state."""
        session = async_get_clientsession(self.hass)
        url = f"{self.web_base_url}{API_HA_STATUS}"
        prev_snapshot = self._state_comparison_tuple()

        async def _do_get() -> None:
            async with session.get(url) as resp:
                if resp.status == 200:
                    payload = await resp.json(content_type=None)
                    if isinstance(payload, dict) and payload.get("ok") is True:
                        self._apply_ha_ext_payload(payload)
                    else:
                        self._clear_ha_ext_state()
                else:
                    self._clear_ha_ext_state()

        try:
            await asyncio.wait_for(_do_get(), timeout=3.0)
        except Exception:  # noqa: BLE001
            self._clear_ha_ext_state()

        changed = self._state_comparison_tuple() != prev_snapshot
        if changed:
            self.async_set_updated_data(self.data)
        return changed

    def _apply_ha_ext_payload(self, payload: dict[str, Any]) -> None:
        """Apply JSON returned by /api/ha/status to self.data."""
        self.data.patch_installed = True
        ver = payload.get("version") or payload.get("patch_version")
        self.data.patch_version = str(ver) if ver is not None else None

        outputs = payload.get("outputs")
        if isinstance(outputs, list):
            for idx in range(min(4, len(outputs))):
                item = outputs[idx]
                if isinstance(item, dict):
                    pin_idx = int(item.get("index", idx))
                    if 0 <= pin_idx < 4:
                        self.data.aux_outputs[pin_idx] = bool(item.get("state", False))
                        lbl = str(item.get("name", "")).strip()
                        if lbl:
                            self.data.output_labels[pin_idx] = lbl
                else:
                    self.data.aux_outputs[idx] = bool(item)
            self.data.io_source = "hardware_gpio"

        labels = payload.get("output_labels")
        if isinstance(labels, list):
            for idx in range(min(4, len(labels))):
                lbl = str(labels[idx]).strip()
                self.data.output_labels[idx] = lbl if lbl else f"OUT{idx}"

        warmup = payload.get("spindle_warmup")
        if isinstance(warmup, dict):
            self.data.spindle_warmup_active = bool(warmup.get("active", False))
            self.data.spindle_warmup_stage = int(warmup.get("stage", 0) or 0)
            rem = warmup.get("remaining_seconds")
            if rem is None:
                rem = warmup.get("time_remaining", 0)
            self.data.spindle_warmup_remaining_seconds = int(rem or 0)
            status_str = warmup.get("status") or warmup.get("status_message") or "Idle"
            self.data.spindle_warmup_status = str(status_str)

        macros = payload.get("macros")
        if isinstance(macros, list):
            normalized_macros: list[dict[str, Any]] = []
            for m in macros:
                if isinstance(m, dict):
                    nm = dict(m)
                    if "path" not in nm and "file_path" in nm:
                        nm["path"] = nm["file_path"] or ""
                    normalized_macros.append(nm)
            self.data.macros = normalized_macros

        self.data.macro_running = bool(payload.get("macro_running", False))

    def _clear_ha_ext_state(self) -> None:
        """Reset HA extension state when patch is not installed or unreachable."""
        self.data.patch_installed = False
        self.data.patch_version = None
        self.data.spindle_warmup_active = False
        self.data.spindle_warmup_stage = 0
        self.data.spindle_warmup_remaining_seconds = 0
        self.data.spindle_warmup_status = "Idle"
        self.data.macro_running = False

    async def _async_post_ha_ext(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a POST request to a /api/ha/* endpoint and return its JSON response."""
        if not self.data.patch_installed:
            await self.async_poll_ha_extension()
            if not self.data.patch_installed:
                raise HomeAssistantError(
                    "Redline CNC Controller Patch is not installed. Press 'Install Controller Patch' first."
                )

        session = async_get_clientsession(self.hass)
        url = f"{self.web_base_url}{path}"

        async def _do_post() -> Any:
            async with session.post(url, json=json_body or {}) as resp:
                return await resp.json(content_type=None)

        try:
            data = await asyncio.wait_for(_do_post(), timeout=5.0)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Failed to call Redline CNC endpoint {path}: {err}"
            ) from err

        if not isinstance(data, dict):
            raise HomeAssistantError(f"Invalid response from {path}: {data}")
        if not data.get("ok", False):
            err_msg = data.get("error") or data.get("detail") or f"Controller rejected request to {path}"
            raise HomeAssistantError(str(err_msg))
        return data

    @property
    def allow_remote_motion(self) -> bool:
        """Return True if remote spindle warmup and macro execution are enabled."""
        if hasattr(self, "config_entry") and self.config_entry is not None:
            return bool(
                self.config_entry.options.get(
                    CONF_ALLOW_REMOTE_MOTION,
                    self.config_entry.data.get(
                        CONF_ALLOW_REMOTE_MOTION,
                        DEFAULT_ALLOW_REMOTE_MOTION,
                    ),
                )
            )
        return DEFAULT_ALLOW_REMOTE_MOTION

    async def async_install_controller_patch(self) -> dict[str, Any]:
        """Install the controller patch over SSH with concurrency protection."""
        async with self._patch_lock:
            result = await async_install_patch_ssh(
                host=self.host,
                port=self.ssh_port,
                username=self.ssh_user,
                password=self.ssh_password,
                restart_rts1=False,
            )
            if not result.get("ok"):
                raise HomeAssistantError(
                    f"Failed to install Redline CNC controller patch: {result.get('error', 'unknown error')}"
                )
            await self.async_poll_ha_extension()
            return result

    async def async_uninstall_controller_patch(self) -> dict[str, Any]:
        """Uninstall the controller patch over SSH and restore vendor-original Web_API.py."""
        async with self._patch_lock:
            result = await async_uninstall_patch_ssh(
                host=self.host,
                port=self.ssh_port,
                username=self.ssh_user,
                password=self.ssh_password,
                restart_rts1=False,
            )
            if not result.get("ok"):
                raise HomeAssistantError(
                    f"Failed to uninstall Redline CNC controller patch: {result.get('error', 'unknown error')}"
                )

            self._clear_ha_ext_state()
            self.async_set_updated_data(self.data)
            return result

    async def async_restart_rts1(self, force: bool = False) -> dict[str, Any]:
        """Safely restart the RTS1 controller process via SSH."""
        async with self._patch_lock:
            if not force:
                if self.data.is_running:
                    raise HomeAssistantError("Cannot restart RTS1 while the machine is actively running or cutting.")
                if self.data.spindle_on:
                    raise HomeAssistantError("Cannot restart RTS1 while the spindle is running.")
                if self.data.macro_running:
                    raise HomeAssistantError("Cannot restart RTS1 while a macro is running.")
                if self.data.spindle_warmup_active:
                    raise HomeAssistantError("Cannot restart RTS1 while spindle warmup is active.")

            result = await async_restart_controller_rts1(
                host=self.host,
                port=self.ssh_port,
                username=self.ssh_user,
                password=self.ssh_password,
                force=force,
            )
            if not result.get("ok"):
                raise HomeAssistantError(
                    f"Failed to restart RTS1: {result.get('error', 'unknown error')}"
                )

            # Wait up to 12 seconds for RTS1 to respawn and resume answering
            for _ in range(12):
                await asyncio.sleep(1.0)
                await self.async_poll_ha_extension()
                if self.data.patch_installed:
                    break

            return result

    async def async_start_spindle_warmup(self) -> dict[str, Any]:
        """Launch the 5-stage Spindle Warmup routine on the controller."""
        if not self.allow_remote_motion:
            raise HomeAssistantError(
                "Remote spindle warmup is disabled in integration options. "
                "Enable 'Allow remote spindle and macro execution' in options to proceed."
            )
        res = await self._async_post_ha_ext(API_HA_WARMUP_START)
        await self.async_poll_ha_extension()
        return res

    async def async_cancel_spindle_warmup(self) -> dict[str, Any]:
        """Cancel an active Spindle Warmup routine and stop the spindle."""
        res = await self._async_post_ha_ext(API_HA_WARMUP_CANCEL)
        await self.async_poll_ha_extension()
        return res

    async def async_set_output(self, pin: int, state: bool) -> dict[str, Any]:
        """Turn hardware auxiliary output OUT0..OUT3 on or off."""
        if pin not in (0, 1, 2, 3):
            raise HomeAssistantError(f"Output pin must be 0, 1, 2, or 3 (got {pin})")
        res = await self._async_post_ha_ext(
            API_HA_OUTPUT,
            {"pin": int(pin), "state": bool(state)},
        )
        outputs = res.get("outputs")
        if isinstance(outputs, list) and len(outputs) > pin:
            for idx in range(min(4, len(outputs))):
                self.data.aux_outputs[idx] = bool(outputs[idx])
        else:
            self.data.aux_outputs[pin] = bool(state)
        self.data.io_source = "hardware_gpio"
        self.async_set_updated_data(self.data)
        return res

    async def async_run_macro(self, macro: int | str) -> dict[str, Any]:
        """Execute a configured macro by 0-based index (0..11), 1-based number, or name."""
        if not self.allow_remote_motion:
            raise HomeAssistantError(
                "Remote macro execution is disabled in integration options. "
                "Enable 'Allow remote spindle and macro execution' in options to proceed."
            )
        target_index: int | None = None
        if isinstance(macro, int):
            target_index = macro
        elif isinstance(macro, str):
            cleaned = macro.strip()
            if cleaned.isdigit():
                num = int(cleaned)
                target_index = num - 1 if 1 <= num <= 12 else num
            else:
                for m in self.data.macros:
                    if str(m.get("name", "")).strip().lower() == cleaned.lower():
                        target_index = int(m["index"])
                        break
                if target_index is None:
                    raise HomeAssistantError(
                        f"Macro '{macro}' not found on controller"
                    )
        if target_index is None or not (0 <= target_index < 12):
            raise HomeAssistantError(
                f"Invalid macro index {target_index}; must be 0..11"
            )

        res = await self._async_post_ha_ext(
            API_HA_MACRO_RUN,
            {"index": int(target_index)},
        )
        await self.async_poll_ha_extension()
        return res

    async def async_abort_macro(self) -> dict[str, Any]:
        """Abort an actively running macro."""
        res = await self._async_post_ha_ext(API_HA_MACRO_ABORT)
        await self.async_poll_ha_extension()
        return res

    async def _async_ws_loop(self) -> None:
        """Background loop maintaining the WebSocket connection with backoff."""
        session = async_get_clientsession(self.hass)
        backoff = 1.0

        while not self._stop_event.is_set():
            try:
                async with session.ws_connect(
                    self.ws_url,
                    heartbeat=20.0,
                    receive_timeout=30.0,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    _LOGGER.info("Connected to Redline CNC WebSocket at %s", self.ws_url)
                    self._set_connected(True)
                    await self.async_request_refresh_all()

                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_text_message(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Redline CNC WebSocket (%s) disconnected: %s; retrying in %.1fs",
                    self.ws_url,
                    err,
                    backoff,
                )
            finally:
                self._ws = None
                if self.data.connected:
                    self._set_connected(False)

            if self._stop_event.is_set():
                break

            try:
                async with asyncio.timeout(backoff):
                    await self._stop_event.wait()
            except TimeoutError:
                pass
            backoff = min(backoff * 2.0, 30.0)

    def _set_connected(self, connected: bool) -> None:
        """Update connection state and notify listeners."""
        self.data.connected = connected
        if not connected:
            self.data.mach_state = "Disconnected"
            self.data.is_running = False
            self.data.tool_change_active = False
            self.data.spindle_on = False
            self.data.vacuum_on = False
            self.data.air_assist_on = False
            self.data.aux_outputs = {k: False for k in self.data.aux_outputs}
            self._clear_ha_ext_state()
            self._requested_downloads.clear()
            self._download_buffers.clear()
        self.async_set_updated_data(self.data)

    def _handle_text_message(self, raw_text: str) -> None:
        """Parse and dispatch an incoming JSON WebSocket text frame."""
        try:
            payload = json.loads(raw_text)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return

        # 1. Fast path for high-frequency camera frames (~10-15 fps)
        if "image_data" in payload and "frame_number" in payload:
            img_b64 = payload.get("image_data")
            if isinstance(img_b64, str) and img_b64:
                self._latest_frame_b64 = img_b64
                self._latest_frame_number = payload.get("frame_number")
                self._last_frame_monotonic = time.monotonic()
                self._frame_event.set()

                width = payload.get("width")
                height = payload.get("height")
                was_available = self.data.camera_available
                self.data.camera_available = True
                if isinstance(width, int):
                    self.data.frame_width = width
                if isinstance(height, int):
                    self.data.frame_height = height
                if not was_available:
                    self.async_set_updated_data(self.data)
            return

        # 2. Command responses (versionInfo, toolTable, file downloads, previews)
        cmd = payload.get("command")
        if cmd == "versionInfo":
            version = payload.get("version")
            if version is not None:
                ver_str = str(version)
                if ver_str != self.data.version:
                    self.data.version = ver_str
                    self.async_set_updated_data(self.data)
            return

        if cmd == "toolTable":
            raw_tools = payload.get("tools")
            if isinstance(raw_tools, list):
                sanitized_tools = []
                for tool in raw_tools:
                    if isinstance(tool, dict):
                        sanitized_tools.append(
                            {
                                k: v
                                for k, v in tool.items()
                                if k != "image_data"
                            }
                        )
                if sanitized_tools != self.data.tools:
                    self.data.tools = sanitized_tools
                    self.async_set_updated_data(self.data)
            return

        if cmd == "startDownload":
            fname = payload.get("fileName")
            if isinstance(fname, str):
                self._download_buffers[fname] = bytearray()
            return

        if cmd == "downloadChunk":
            fname = payload.get("fileName")
            chunk_b64 = payload.get("chunkData")
            if isinstance(fname, str) and isinstance(chunk_b64, str):
                buf = self._download_buffers.setdefault(fname, bytearray())
                try:
                    buf.extend(base64.b64decode(chunk_b64))
                except Exception:  # noqa: BLE001
                    pass
            return

        if cmd == "downloadComplete":
            fname = payload.get("fileName")
            if isinstance(fname, str) and fname in self._download_buffers:
                raw_bytes = bytes(self._download_buffers.pop(fname))
                text = raw_bytes.decode("utf-8", errors="replace")
                basename = fname.rsplit("/", 1)[-1]
                timeline = GcodeProgramTimeline(text)
                self._gcode_timelines[fname] = timeline
                self._gcode_timelines[basename] = timeline
                _LOGGER.debug(
                    "Parsed G-code modal IO timeline for %s (%d lines)",
                    fname,
                    timeline.total_lines,
                )
                if self.data.total_lines == 0 and basename == self.data.file_name:
                    self.data.total_lines = timeline.total_lines
                if self._update_modal_io_state():
                    self.async_set_updated_data(self.data)
            return

        # 3. File tree response (from refreshFileList)
        if "name" in payload and "children" in payload:
            paths: dict[str, str] = {}
            self._collect_file_paths(payload, "", paths)
            changed = (
                paths != self.data.file_paths
                or len(paths) != self.data.file_count
            )
            self.data.file_paths = paths
            self.data.file_count = len(paths)
            self._maybe_request_active_gcode_download()
            if changed:
                self.async_set_updated_data(self.data)
            return

        # 4. Machine status broadcast
        if any(
            k in payload
            for k in (
                "mach_state",
                "file_name",
                "progress",
                "mach_pos",
                "wcs_offset",
                "current_line",
            )
        ):
            if self._apply_status_payload(payload):
                self.async_set_updated_data(self.data)

    def _collect_file_paths(
        self,
        node: dict[str, Any],
        parent_path: str,
        out: dict[str, str],
    ) -> None:
        """Recursively flatten the RTS-X file tree into a filename -> path lookup."""
        name = str(node.get("name", ""))
        if not name:
            return
        full_path = f"{parent_path}/{name}" if parent_path else name
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    self._collect_file_paths(child, full_path, out)
        else:
            out[name] = full_path
            out[full_path] = full_path

    def _maybe_request_active_gcode_download(self) -> None:
        """Download the currently loaded G-code file once so modal IO can be tracked."""
        if not self.enable_gcode_io:
            return
        fname = self.data.file_name
        if not fname or fname in self._gcode_timelines:
            return
        full_path = self.data.file_paths.get(fname, f"gcode/{fname}")
        if full_path in self._requested_downloads:
            return
        self._requested_downloads.add(full_path)
        self.hass.async_create_task(
            self.async_send_command(
                {"command": CMD_DOWNLOAD_FILE, "filePath": full_path}
            )
        )

    def _update_eta(self) -> bool:
        """Compute and update the ETA timestamp and remaining duration from controller telemetry."""
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        now_mono = time.monotonic()
        prev_eta = self.data.eta

        if not self.data.is_running or not self.data.file_name:
            if not self.data.is_running and self.data.progress < 0.999:
                self.data.remaining_seconds = 0
                self.data.elapsed_seconds = 0
            return self.data.eta != prev_eta

        # Compute real elapsed time since job started
        if self._job_start_mono is not None:
            elapsed_s = max(1.0, now_mono - self._job_start_mono)
        else:
            elapsed_s = 1.0
        self.data.elapsed_seconds = int(elapsed_s)

        # 1. Check if controller's elapsed_eta provides an explicit ETA duration
        parsed_s = _parse_eta_seconds(self.data.elapsed_eta, self.data.progress)
        remaining_s: float = 0.0

        if parsed_s > 0:
            remaining_s = float(parsed_s)
            self.data.remaining_seconds = int(remaining_s)
        else:
            # 2. Derive remaining time from controller progress
            prog = self.data.progress
            if prog <= 0.0 and self.data.total_lines > 0 and self.data.current_line_number > 0:
                prog = self.data.current_line_number / self.data.total_lines

            if prog > 0.002 and elapsed_s >= 2.0:
                total_est_s = elapsed_s / prog
                remaining_s = max(0.0, total_est_s - elapsed_s)
                self.data.remaining_seconds = int(remaining_s)
            elif self.data.remaining_seconds > 0:
                remaining_s = float(self.data.remaining_seconds)

        if remaining_s > 0.0:
            target_eta = now_utc + timedelta(seconds=int(round(remaining_s)))
            # Initialize or update with a 30s hysteresis window to avoid per-second timestamp jitter
            if (
                self.data.eta is None
                or self.data.eta <= now_utc
                or abs((target_eta - self.data.eta).total_seconds()) > 30
            ):
                self.data.eta = target_eta
                return True

        return self.data.eta != prev_eta

    def _apply_status_payload(self, payload: dict[str, Any]) -> bool:
        """Apply a status JSON message to `self.data`, returning True if state changed."""
        prev_snapshot = self._state_comparison_tuple()

        if isinstance(payload.get("file_name"), str):
            new_file = payload["file_name"]
            if new_file != self.data.file_name:
                self.data.file_name = new_file
                self.data.eta = None
                self._eta_locked_for_file = None
                self._last_eta_duration_s = None
                self._job_start_mono = None
                self._job_start_utc = None
                self.data.job_start_utc = None
                self.data.remaining_seconds = 0
                self.data.elapsed_seconds = 0
                self._job_line_advanced = False
                self._maybe_request_active_gcode_download()

        if isinstance(payload.get("file_info"), str):
            self.data.file_info = payload["file_info"]
            m_lines = _FILE_INFO_LINES_RE.search(self.data.file_info)
            if m_lines:
                try:
                    self.data.total_lines = int(m_lines.group(1))
                except ValueError:
                    pass

        if isinstance(payload.get("progress"), (int, float)):
            prog = float(payload["progress"])
            self.data.progress = prog
            self.data.progress_percent = round(prog * 100.0, 1)

        if isinstance(payload.get("current_line"), str):
            cur_line = payload["current_line"]
            self.data.current_line = cur_line
            cur_num, tot_num = _parse_current_line(cur_line)
            self.data.current_line_number = cur_num
            self.data.reported_line_total = tot_num
            if tot_num > 0:
                self.data.total_lines = tot_num

        if isinstance(payload.get("elapsed_eta"), str):
            self.data.elapsed_eta = payload["elapsed_eta"]

        if isinstance(payload.get("mach_state"), str):
            mach_state = payload["mach_state"]
            self.data.mach_state = mach_state
            state_lower = mach_state.strip().lower()
            self.data.estop_active = "e-stop" in state_lower or "estop" in state_lower
            is_tc_mach_state = (
                "tool change" in state_lower
                or "toolchange" in state_lower
                or ("tool" in state_lower and "change" in state_lower)
            )
            is_non_job_state = any(
                kw in state_lower
                for kw in (
                    "prob",
                    "hom",
                    "jog",
                    "mov",
                    "mdi",
                    "hold",
                    "paus",
                    "stop",
                    "ready",
                    "idle",
                    "alarm",
                    "err",
                    "off",
                    "disconnect",
                    "tool",
                    "change",
                )
            )
            if (
                self.data.estop_active
                or is_non_job_state
                or state_lower in IDLE_MACHINE_STATES
            ):
                self.data.is_running = False
            elif state_lower in RUNNING_MACHINE_STATES:
                self.data.is_running = True
            else:
                self.data.is_running = False

            terminal_idle = any(
                st in state_lower
                for st in (
                    "ready",
                    "idle",
                    "stop",
                    "alarm",
                    "err",
                    "disconnect",
                    "off",
                    "e-stop",
                    "estop",
                )
            )
            if (terminal_idle and not is_tc_mach_state) or not self.data.connected:
                self.data.tool_change_active = False
            elif is_tc_mach_state:
                self.data.tool_change_active = True

        if self.data.is_running:
            if not self._last_job_running or self._job_start_mono is None:
                self._job_start_mono = time.monotonic()
                self._job_start_utc = datetime.now(timezone.utc).replace(microsecond=0)
                self.data.job_start_utc = self._job_start_utc
                self.data.eta = None
            self._last_job_running = True
        else:
            if self._last_job_running:
                if self.data.progress >= 0.999 or (
                    self.data.total_lines > 0
                    and self.data.current_line_number >= self.data.total_lines
                ):
                    self.data.eta = datetime.now(timezone.utc).replace(microsecond=0)
                else:
                    self.data.eta = None
            self._last_job_running = False
            self._job_start_mono = None
            self._job_start_utc = None
            self.data.job_start_utc = None

        if not self.data.is_running:
            self._idle_line_number = self.data.current_line_number
            self._job_line_advanced = False
        elif (
            self.data.current_line_number > 0
            and self.data.current_line_number != self._idle_line_number
        ):
            self._job_line_advanced = True

        self._update_eta()

        if isinstance(payload.get("units"), str):
            units = payload["units"].lower()
            self.data.units = units
            self.data.unit_symbol = "in" if units == "imperial" else "mm"

        scale = 25.4 if self.data.units == "imperial" else 1.0

        if isinstance(payload.get("mach_pos"), list) and len(payload["mach_pos"]) >= 4:
            try:
                self.data.mach_pos = tuple(
                    round(float(payload["mach_pos"][i]) / scale, 3)
                    for i in range(4)
                )  # type: ignore[assignment]
            except (TypeError, ValueError):
                pass

        if (
            isinstance(payload.get("wcs_offset"), list)
            and len(payload["wcs_offset"]) >= 4
        ):
            try:
                self.data.wcs_offset = tuple(
                    round(float(payload["wcs_offset"][i]) / scale, 4)
                    for i in range(4)
                )  # type: ignore[assignment]
            except (TypeError, ValueError):
                pass

        self.data.work_pos = tuple(
            round(self.data.mach_pos[i] - self.data.wcs_offset[i], 3)
            for i in range(4)
        )  # type: ignore[assignment]

        # Check if camera stream has gone stale (>15s without frames)
        if (
            self.data.camera_available
            and self._last_frame_monotonic > 0
            and (time.monotonic() - self._last_frame_monotonic) > 15.0
        ):
            self.data.camera_available = False

        # Check for direct IO fields in payload first; fall back to G-code modal IO
        if not self._apply_direct_io_payload(payload):
            self._update_modal_io_state()

        return self._state_comparison_tuple() != prev_snapshot

    def _apply_direct_io_payload(self, payload: dict[str, Any]) -> bool:
        """Extract direct hardware IO fields if present in the WebSocket status payload."""
        found_direct = False

        io_dict = payload.get("io") or payload.get("ios") or payload.get("outputs")
        if isinstance(io_dict, dict):
            found_direct = True
            if "vacuum" in io_dict or "flood" in io_dict or "coolant" in io_dict:
                self.data.vacuum_on = bool(
                    io_dict.get("vacuum")
                    or io_dict.get("flood")
                    or io_dict.get("coolant")
                )
            if "air_assist" in io_dict or "mist" in io_dict:
                self.data.air_assist_on = bool(
                    io_dict.get("air_assist") or io_dict.get("mist")
                )
            if "spindle" in io_dict:
                self.data.spindle_on = bool(io_dict.get("spindle"))
            for pin in (0, 1, 2, 3):
                for key in (f"aux_{pin}", f"aux{pin}", f"p{pin}", str(pin)):
                    if key in io_dict:
                        self.data.aux_outputs[pin] = bool(io_dict[key])

        for top_key, attr in (
            ("vacuum", "vacuum_on"),
            ("flood", "vacuum_on"),
            ("dust_collector", "vacuum_on"),
            ("air_assist", "air_assist_on"),
            ("mist", "air_assist_on"),
            ("spindle", "spindle_on"),
        ):
            if top_key in payload and isinstance(payload[top_key], (bool, int)):
                setattr(self.data, attr, bool(payload[top_key]))
                found_direct = True

        if "spindle_rpm" in payload or "spindle_speed" in payload:
            raw_rpm = payload.get("spindle_rpm", payload.get("spindle_speed"))
            if isinstance(raw_rpm, (int, float)):
                self.data.spindle_rpm = float(raw_rpm)
                found_direct = True

        if "feed_rate" in payload or "feed" in payload:
            raw_feed = payload.get("feed_rate", payload.get("feed"))
            if isinstance(raw_feed, (int, float)):
                self.data.feed_rate = float(raw_feed)
                found_direct = True

        if found_direct:
            self.data.io_source = "websocket"
        return found_direct

    def _is_gcode_job_active(self, timeline: GcodeProgramTimeline) -> bool:
        """Return True only when the loaded G-code file is actively executing (not HMI probing)."""
        if not self.data.is_running or not self.data.file_name:
            return False
        if self.data.current_line_number <= 0:
            return False
        # If the controller reports a total line count that doesn't match the loaded file,
        # it is executing an internal macro/subroutine (such as an HMI XYZ probe routine).
        if self.data.reported_line_total > 0 and timeline.total_lines > 0:
            if (
                timeline.total_lines >= 100
                and self.data.reported_line_total < min(80, int(timeline.total_lines * 0.25))
            ):
                return False
            if abs(self.data.reported_line_total - timeline.total_lines) > max(
                150, int(timeline.total_lines * 0.35)
            ):
                return False
        # Require evidence that the G-code program itself is progressing rather than sitting
        # at a stale idle line number while HMI probing moves the axes.
        return (
            self._job_line_advanced
            or self.data.progress > 0.0
            or _parse_eta_seconds(self.data.elapsed_eta, self.data.progress) > 0
        )

    def _update_modal_io_state(self) -> bool:
        """Update IO and spindle/feed state from the active G-code timeline at current_line."""
        prev = (
            self.data.spindle_on,
            self.data.spindle_cw,
            self.data.spindle_rpm,
            self.data.feed_rate,
            self.data.active_tool,
            self.data.vacuum_on,
            self.data.air_assist_on,
            self.data.tool_change_active,
            dict(self.data.aux_outputs),
        )

        modal: GcodeModalState | None = None
        timeline = self._gcode_timelines.get(self.data.file_name)
        if (
            self.enable_gcode_io
            and timeline is not None
            and self.data.current_line_number > 0
        ):
            modal = timeline.state_at_line(
                self.data.current_line_number
            )
            self.data.spindle_rpm = modal.spindle_rpm
            self.data.feed_rate = modal.feed_rate
            self.data.active_tool = modal.active_tool
            self.data.spindle_cw = modal.spindle_cw

            if self._is_gcode_job_active(timeline):
                self.data.spindle_on = modal.spindle_on
                self.data.vacuum_on = modal.vacuum_on
                self.data.air_assist_on = modal.air_assist_on
                if not self.data.patch_installed:
                    for pin in (0, 1):
                        self.data.aux_outputs[pin] = modal.aux_outputs.get(pin, False)
            else:
                self.data.spindle_on = False
                self.data.vacuum_on = False
                self.data.air_assist_on = False
                if not self.data.patch_installed:
                    for pin in (0, 1):
                        self.data.aux_outputs[pin] = False
            if not self.data.patch_installed:
                self.data.io_source = "gcode_modal"
        else:
            if not self.data.is_running:
                self.data.spindle_on = False
                self.data.vacuum_on = False
                self.data.air_assist_on = False
                if not self.data.patch_installed:
                    for pin in (0, 1):
                        self.data.aux_outputs[pin] = False

        state_lower = self.data.mach_state.strip().lower()
        is_tc_mach_state = (
            "tool change" in state_lower
            or "toolchange" in state_lower
            or ("tool" in state_lower and "change" in state_lower)
        )
        terminal_idle = any(
            st in state_lower
            for st in (
                "ready",
                "idle",
                "stop",
                "alarm",
                "err",
                "disconnect",
                "off",
                "e-stop",
                "estop",
            )
        )

        if (terminal_idle and not is_tc_mach_state) or not self.data.connected:
            self.data.tool_change_active = False
        elif is_tc_mach_state:
            self.data.tool_change_active = True
        elif modal is not None and modal.in_tool_change:
            self.data.tool_change_active = True
        elif self.data.is_running:
            self.data.tool_change_active = False

        curr = (
            self.data.spindle_on,
            self.data.spindle_cw,
            self.data.spindle_rpm,
            self.data.feed_rate,
            self.data.active_tool,
            self.data.vacuum_on,
            self.data.air_assist_on,
            self.data.tool_change_active,
            dict(self.data.aux_outputs),
        )
        return curr != prev

    def _state_comparison_tuple(self) -> tuple[Any, ...]:
        """Return a tuple of state fields used to detect meaningful changes."""
        return (
            self.data.connected,
            self.data.version,
            self.data.mach_state,
            self.data.is_running,
            self.data.estop_active,
            self.data.tool_change_active,
            self.data.file_name,
            self.data.file_info,
            self.data.progress_percent,
            self.data.current_line_number,
            self.data.total_lines,
            self.data.eta,
            self.data.units,
            self.data.mach_pos,
            self.data.work_pos,
            self.data.spindle_on,
            self.data.spindle_rpm,
            self.data.feed_rate,
            self.data.active_tool,
            self.data.vacuum_on,
            self.data.air_assist_on,
            tuple(sorted(self.data.aux_outputs.items())),
            tuple(sorted(self.data.output_labels.items())),
            self.data.camera_available,
            self.data.remaining_seconds,
            self.data.elapsed_seconds,
            self.data.patch_installed,
            self.data.patch_version,
            self.data.spindle_warmup_active,
            self.data.spindle_warmup_stage,
            self.data.spindle_warmup_remaining_seconds,
            self.data.spindle_warmup_status,
            self.data.macro_running,
            tuple(
                (m.get("index"), m.get("name"), m.get("path"))
                for m in self.data.macros
            ),
        )


def _parse_current_line(current_line_str: str) -> tuple[int, int]:
    """Parse '3685 / 5516' into (3685, 5516)."""
    parts = current_line_str.split("/")
    if len(parts) == 2:
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            return 0, 0
    try:
        return int(current_line_str.strip()), 0
    except ValueError:
        return 0, 0


def _parse_eta_seconds(elapsed_eta_str: str, progress: float = 0.0) -> int:
    """Parse an 'HH:MM:SS', 'elapsed / HH:MM:SS', or 'Xh Ym Zs' string into remaining seconds."""
    if not elapsed_eta_str:
        return 0

    tokens: list[int] = []
    for p1, p2, p3 in _TIME_TOKEN_RE.findall(elapsed_eta_str):
        try:
            if p3 != "":
                sec = int(p1) * 3600 + int(p2) * 60 + int(float(p3))
            else:
                sec = int(p1) * 60 + int(p2)
            tokens.append(sec)
        except ValueError:
            continue

    if len(tokens) >= 2:
        elapsed_s = tokens[0]
        eta_s = tokens[-1]
        if eta_s > 0:
            return eta_s
        if elapsed_s > 0 and 0.0 < progress < 1.0:
            return int(round(elapsed_s * (1.0 - progress) / progress))
        return 0

    if len(tokens) == 1:
        single_s = tokens[0]
        # If string had a '/' separator where the ETA half was '--:--:--' and only elapsed matched
        if "/" in elapsed_eta_str:
            right_half = elapsed_eta_str.split("/", 1)[1]
            if not _TIME_TOKEN_RE.search(right_half):
                if single_s > 0 and 0.0 < progress < 1.0:
                    return int(round(single_s * (1.0 - progress) / progress))
                return 0
        return single_s

    m_hms = _HMS_UNIT_RE.search(elapsed_eta_str)
    if m_hms and any(m_hms.groups()):
        h = int(m_hms.group(1) or 0)
        m = int(m_hms.group(2) or 0)
        s = int(float(m_hms.group(3) or 0))
        return h * 3600 + m * 60 + s

    return 0
