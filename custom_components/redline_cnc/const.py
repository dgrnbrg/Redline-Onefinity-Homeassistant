"""Constants for the Redline CNC (RTS-X) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "redline_cnc"
DEFAULT_NAME = "Redline CNC"
DEFAULT_PORT = 8080
DEFAULT_WEB_PORT = 8000
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "rtcnc"
DEFAULT_SSH_PASSWORD = "rtcnc"

CONF_ENABLE_GCODE_IO = "enable_gcode_io"
DEFAULT_ENABLE_GCODE_IO = True
CONF_ALLOW_REMOTE_MOTION = "allow_remote_motion"
DEFAULT_ALLOW_REMOTE_MOTION = False
CONF_WEB_PORT = "web_port"
CONF_SSH_PORT = "ssh_port"
CONF_SSH_USER = "ssh_user"
CONF_SSH_PASSWORD = "ssh_password"

API_HA_STATUS = "/api/ha/status"
API_HA_WARMUP_START = "/api/ha/spindle-warmup/start"
API_HA_WARMUP_CANCEL = "/api/ha/spindle-warmup/cancel"
API_HA_OUTPUT = "/api/ha/output"
API_HA_MACRO_RUN = "/api/ha/macro/run"
API_HA_MACRO_ABORT = "/api/ha/macro/abort"

SERVICE_RUN_MACRO = "run_macro"
SERVICE_ABORT_MACRO = "abort_macro"
SERVICE_SET_OUTPUT = "set_output"
SERVICE_START_SPINDLE_WARMUP = "start_spindle_warmup"
SERVICE_CANCEL_SPINDLE_WARMUP = "cancel_spindle_warmup"
SERVICE_INSTALL_PATCH = "install_patch"
SERVICE_UNINSTALL_PATCH = "uninstall_patch"
SERVICE_RESTART_RTS1 = "restart_rts1"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SWITCH,
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
    "probing",
    "probe",
    "homing",
    "home",
    "jogging",
    "jog",
    "moving",
    "mdi",
    "tool change",
    "toolchange",
    "tool_change",
}

RUNNING_MACHINE_STATES: set[str] = {
    "running",
    "run",
    "cutting",
}

MACHINE_STATE_OPTIONS: tuple[str, ...] = (
    "Ready",
    "Running",
    "Cutting",
    "Paused",
    "Hold",
    "Probing",
    "Homing",
    "Jogging",
    "Moving",
    "MDI",
    "Tool Change",
    "Stopped",
    "E-Stop",
    "Alarm",
    "Error",
    "Disconnected",
    "Unknown",
)

AXIS_NAMES: tuple[str, ...] = ("X", "Y", "Z", "A")
