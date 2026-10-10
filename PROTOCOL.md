# Redline CNC (RTS-X) WebSocket Protocol Specification

This document details the reverse-engineered WebSocket protocol used by the **Redline CNC / RealTime CNC RTS-X Web Interface** (verified against RTS-X Server `V1.6.3`).

---

## 1. Network Architecture

| Port | Server Header | Purpose |
| :--- | :--- | :--- |
| `80` / `8000` | `uvicorn` (FastAPI) | Serves static assets for the Flutter Web frontend (`main.dart.js`, `index.html`, `/version.json`). |
| `8080` | `RTS-X Server` | Unauthenticated JSON-over-WebSocket endpoint (`ws://<host>:8080`) for real-time telemetry, camera streaming, file management, and tool table management. |

When the Flutter web frontend loads from `http://<host>/`, it connects to `ws://<host>:8080` (or `wss://<host>:8080` if loaded over HTTPS) and immediately sends two initialization commands:
1. `{"command": "refreshStatus"}`
2. `{"command": "requestVersion"}`

---

## 2. Server-to-Client Messages (Inbound Telemetry)

All WebSocket frames are UTF-8 JSON text frames (`opcode = 0x1`).

### 2.1 Machine Status Broadcast (~3–4 Hz & on `refreshStatus`)
The server continuously broadcasts machine status without requiring client polling:

```json
{
  "file_name": "trapezoid_calibrate_bed.ngc",
  "file_info": "5516 lines [122.5 kB]",
  "progress": 0.0,
  "current_line": "3685 / 5516",
  "elapsed_eta": "00:00:00",
  "mach_state": "Ready",
  "mach_pos": [512.064, 94.112, -93.755, 0.0],
  "wcs_offset": [512.064, 93.528, -17.11, 0.0],
  "units": "imperial"
}
```

#### Field Semantics & Coordinate Math
* **`file_name`** (`string`): Name of the currently loaded G-code file.
* **`file_info`** (`string`): Summary of total line count and file size.
* **`progress`** (`float`, `0.0`–`1.0`): Fractional completion (`progress * 100` for percentage).
* **`current_line`** (`string`): Current G-code line and total lines formatted as `"<current> / <total>"`.
* **`elapsed_eta`** (`string`): Elapsed/ETA timer formatted as `"HH:MM:SS"`. *(Sent by `RTS-X Server` v1.6.3 even though not rendered in the web UI).*
* **`mach_state`** (`string`): Controller state string (e.g., `"Ready"`, `"Running"`, `"Homing"`, `"Hold"`, `"E-Stop"`). The web UI flags emergency stop whenever `mach_state.toLowerCase()` contains `"e-stop"`.
* **`units`** (`string`): `"imperial"` (inches) or `"metric"` (millimeters).
* **`mach_pos`** (`float[4]`): `[X, Y, Z, A]` raw machine coordinates in millimeters (`mm`).
  * When `units == "imperial"`, the client divides each coordinate by `25.4` (`mach_pos[i] / 25.4`).
* **`wcs_offset`** (`float[4]`): `[X, Y, Z, A]` Work Coordinate System offset in millimeters (`mm`).
  * When `units == "imperial"`, the client divides each offset by `25.4`.
  * **Work Position (`WPos`)** is computed client-side as:
    $$\text{work\_pos}[i] = \text{round}(\text{mach\_pos\_scaled}[i] - \text{wcs\_offset\_scaled}[i], 3)$$

### 2.2 Camera Feed Broadcast (~10–15 fps)
When a USB camera is connected to the RTS-X controller, the server pushes JPEG frames encoded as Base64 strings over the same WebSocket connection:

```json
{
  "frame_number": 20535,
  "image_data": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBD...",
  "width": 640,
  "height": 480
}
```

* **`frame_number`** (`int`): Monotonically increasing frame counter.
* **`image_data`** (`string`): Base64-encoded JPEG image payload.
* **`width` / `height`** (`int`): Frame dimensions in pixels (`640x480`).

### 2.3 Version Info Response
Sent in reply to `{"command": "requestVersion"}`:

```json
{
  "command": "versionInfo",
  "version": "1.6.3"
}
```

### 2.4 Tool Table Response
Sent in reply to `{"command": "requestToolTable"}`:

```json
{
  "command": "toolTable",
  "tools": [
    {
      "tool_number": 1,
      "description": "Default tool",
      "atc_assignment": 0,
      "atc_slot": 0,
      "length": 0,
      "diameter": 0,
      "image_path": "",
      "image_data": "iVBORw0KGgoAAAANSUhEUg..."
    }
  ]
}
```

### 2.5 File Tree Response
Sent in reply to `{"command": "refreshFileList"}`:

```json
{
  "name": "gcode",
  "size": 0,
  "modified": "2026-10-10 16:46:22",
  "children": [
    {
      "name": "trapezoid_calibrate_bed.ngc",
      "size": 125404,
      "modified": "2026-10-10 16:09:54"
    }
  ]
}
```
Folders contain a `"children"` array; files omit `"children"`.

### 2.6 File Preview & Download Responses
* **File Preview** (reply to `{"command": "previewFile", "filePath": "<path>"}`):
  ```json
  {
    "command": "filePreview",
    "chunkData": "<base64 UTF-8 preview>"
  }
  ```
* **File Download** (reply to `{"command": "downloadFile", "filePath": "<path>"}`):
  1. `{"command": "startDownload", "fileName": "<path>", "totalSize": 125404}`
  2. One or more `{"command": "downloadChunk", "fileName": "<path>", "chunkData": "<base64>"}`
  3. `{"command": "downloadComplete", "fileName": "<path>"}`

---

## 3. Client-to-Server Commands

| Command | Payload Schema | Description |
| :--- | :--- | :--- |
| `refreshStatus` | `{"command": "refreshStatus"}` | Request immediate machine status broadcast |
| `requestVersion` | `{"command": "requestVersion"}` | Request controller software version (`versionInfo`) |
| `requestToolTable` | `{"command": "requestToolTable"}` | Request tool table (`toolTable`) |
| `refreshFileList` | `{"command": "refreshFileList"}` | Request recursive G-code directory tree |
| `previewFile` | `{"command": "previewFile", "filePath": "gcode/file.ngc"}` | Request first chunk preview of a file |
| `downloadFile` | `{"command": "downloadFile", "filePath": "gcode/file.ngc"}` | Stream full file download in Base64 chunks |
| `downloadCancel` | `{"command": "downloadCancel", "fileName": "gcode/file.ngc"}` | Cancel an in-progress download |
| `startUpload` | `{"command": "startUpload", "fileName": "gcode/file.ngc", "totalSize": 1234, "checksum": "<md5>"}` | Initiate chunked file upload (64 KB chunks) |
| `uploadChunk` | `{"command": "uploadChunk", "fileName": "gcode/file.ngc", "chunkData": "<base64>", "offset": 0}` | Send 64 KB chunk (1 ms pacing between chunks) |
| `finishUpload` | `{"command": "finishUpload", "filePath": "gcode/file.ngc"}` | Complete file upload |
| `uploadCancel` | `{"command": "uploadCancel", "filePath": "gcode/file.ngc"}` | Abort file upload |
| `deleteFile` | `{"command": "deleteFile", "filePath": "gcode/file.ngc"}` | Delete a file |
| `deleteFolder` | `{"command": "deleteFolder", "folderPath": "gcode/sub"}` | Delete a folder |
| `moveFile` | `{"command": "moveFile", "sourcePath": "gcode/a.ngc", "destPath": "gcode/sub/a.ngc"}` | Move or rename a file/folder |
| `createFolder` | `{"command": "createFolder", "folderPath": "gcode/sub"}` | Create a folder |
| `addTool` | `{"command": "addTool"}` | Add a new tool entry to the tool table |
| `deleteTool` | `{"command": "deleteTool", "tool_number": 2}` | Delete tool by `tool_number` |
| `changeToolNum` | `{"command": "changeToolNum", "old_number": 2, "new_number": 3}` | Renumber a tool (`1..N`) |
| `updateTool` | `{"command": "updateTool", "tool_number": 1, "field": "description", "value": "1/4 Endmill"}` | Update tool field (`description`, `atc_assignment` `0..4`, `atc_slot` `0..4`) |
| `updateToolImage` | `{"command": "updateToolImage", "tool_number": 1, "image_data": "<base64 PNG>"}` | Upload a `<=500x500` PNG thumbnail for a tool |

---

## 4. Hardware I/O State Note (Vacuum / Air Assist / Spindle)

In **RTS-X Server `V1.6.3`**, the WebSocket status payload does not yet include raw GPIO pin states. To provide immediate, reliable Home Assistant entities for shop-vac, dust collection, air assist, and spindle status:

1. **Direct WebSocket I/O Support**: If future RTS-X firmware versions include `io`, `ios`, `outputs`, `vacuum`, `flood`, `air_assist`, `mist`, or `spindle` fields in the status JSON, the integration reads them directly (`source: websocket`).
2. **Real-Time G-code Modal State Tracking**: Whenever a G-code file is loaded (`file_name`), the integration automatically downloads the G-code file over the WebSocket (`downloadFile`), pre-indexes all modal I/O transitions (`M3`/`M4`/`M5` + `S<rpm>`, `M7` Mist/Air Assist, `M8` Flood/Vacuum, `M9` Coolant Off, `M62`–`M65 P0/P1` Digital Aux Outputs, `M2`/`M30` Program End), and evaluates the active modal state at `current_line` whenever the machine is running (`source: gcode_modal`).
