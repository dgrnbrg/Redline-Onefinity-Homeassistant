"""Constants for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "redline_cnc"
DEFAULT_NAME = "Redline CNC"
DEFAULT_PORT = 8080

CONF_ENABLE_GCODE_IO = "enable_gcode_io"
DEFAULT_ENABLE_GCODE_IO = True

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
]

# WebSocket commands supported by RTS-X Server
CMD_REFRESH_STATUS = "refreshStatus"
CMD_REQUEST_VERSION = "requestVersion"
CMD_REQUEST_TOOL_TABLE = "requestToolTable"
CMD_REFRESH_FILE_LIST = "refreshFileList"
CMD_PREVIEW_FILE = "previewFile"
CMD_DOWNLOAD_FILE = "downloadFile"
CMD_DOWNLOAD_CANCEL = "downloadCancel"
CMD_START_UPLOAD = "startUpload"
CMD_UPLOAD_CHUNK = "uploadChunk"
CMD_FINISH_UPLOAD = "finishUpload"
CMD_UPLOAD_CANCEL = "uploadCancel"
CMD_DELETE_FILE = "deleteFile"
CMD_DELETE_FOLDER = "deleteFolder"
CMD_MOVE_FILE = "moveFile"
CMD_CREATE_FOLDER = "createFolder"
CMD_ADD_TOOL = "addTool"
CMD_DELETE_TOOL = "deleteTool"
CMD_CHANGE_TOOL_NUM = "changeToolNum"
CMD_UPDATE_TOOL = "updateTool"
CMD_UPDATE_TOOL_IMAGE = "updateToolImage"

# Machine states considered idle/stopped vs running
IDLE_MACHINE_STATES: set[str] = {
    "ready",
    "idle",
    "e-stop",
    "estop",
    "emergency stop",
    "stopped",
    "offline",
    "disconnected",
    "alarm",
    "error",
    "hold",
    "paused",
    "pause",
    "unknown",
}

RUNNING_MACHINE_STATES: set[str] = {
    "running",
    "run",
    "cutting",
    "moving",
    "jogging",
    "jog",
    "homing",
    "home",
    "probing",
    "probe",
    "spindle",
}

AXIS_NAMES: tuple[str, ...] = ("X", "Y", "Z", "A")
