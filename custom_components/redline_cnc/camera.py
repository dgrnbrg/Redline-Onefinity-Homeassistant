"""Camera platform for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RedlineCncConfigEntry
from .coordinator import RedlineCncCoordinator
from .entity import RedlineCncEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RedlineCncConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Redline CNC camera from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities([RedlineCncCamera(coordinator, entry.entry_id)])


class RedlineCncCamera(RedlineCncEntity, Camera):
    """Representation of the Redline CNC WebSocket JPEG Camera Feed."""

    _attr_name = "Camera Feed"
    _attr_icon = "mdi:cctv"
    _attr_frame_interval = 0.1

    def __init__(
        self,
        coordinator: RedlineCncCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the Redline CNC camera."""
        RedlineCncEntity.__init__(self, coordinator, entry_id, "camera_feed")
        Camera.__init__(self)
        self._attr_is_streaming = True

    @property
    def available(self) -> bool:
        """Return True if the controller is connected and camera frames are available."""
        return (
            self.coordinator.data.connected
            and self.coordinator.data.camera_available
        )

    @property
    def is_on(self) -> bool:
        """Return True if camera is actively receiving frames."""
        return self.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return camera metadata attributes."""
        return {
            "frame_number": self.coordinator.last_frame_number,
            "width": self.coordinator.data.frame_width,
            "height": self.coordinator.data.frame_height,
        }

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a still JPEG image from the Redline CNC WebSocket stream."""
        image = self.coordinator.get_latest_camera_image()
        if image is not None:
            return image
        if self.coordinator.data.connected:
            return await self.coordinator.async_wait_for_next_frame(timeout=2.0)
        return None

    async def handle_async_mjpeg_stream(
        self,
        request: web.Request,
    ) -> web.StreamResponse | None:
        """Generate an HTTP MJPEG stream directly from incoming WebSocket JPEG frames."""
        response = web.StreamResponse()
        response.content_type = "multipart/x-mixed-replace;boundary=--frame"
        await response.prepare(request)

        last_sent_frame: int | None = None
        try:
            while self.coordinator.data.connected:
                frame_bytes = self.coordinator.get_latest_camera_image()
                cur_num = self.coordinator.last_frame_number
                if frame_bytes is not None and cur_num != last_sent_frame:
                    last_sent_frame = cur_num
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("ascii")
                    )
                    await response.write(header + frame_bytes + b"\r\n")
                await self.coordinator.async_wait_for_next_frame(timeout=1.0)
        except (ConnectionResetError, RuntimeError):
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("MJPEG stream closed: %s", err)

        return response
