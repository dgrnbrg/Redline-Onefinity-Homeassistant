# Redline CNC (RTS-X) — Home Assistant / HACS Integration

A Home Assistant custom integration (HACS-compatible) for the **Redline CNC / RealTime CNC RTS-X Controller**.

It connects via local push WebSocket (`ws://<host>:8080`) to stream real-time machine state, job progress, coordinates, live camera feed, and I/O states (Spindle, Vacuum / Flood `M8`, Air Assist / Mist `M7`, and Aux Outputs `M62`–`M65`).

See [PROTOCOL.md](PROTOCOL.md) for the complete reverse-engineered WebSocket protocol specification.

---

## Features & Entities

### Sensors (`sensor`)
* **Machine State** (`sensor.redline_cnc_machine_state`): `"Ready"`, `"Running"`, `"E-Stop"`, `"Disconnected"`, etc.
* **Job Progress** (`sensor.redline_cnc_job_progress`): `0.0%` – `100.0%` (with attributes `current_line`, `current_line_number`, `total_lines`, `elapsed_eta`).
* **Current File** (`sensor.redline_cnc_current_file`): Currently loaded `.ngc` file name (with `file_info`, `full_path`, `total_files_on_controller`).
* **Current Line** (`sensor.redline_cnc_current_line`): Formatted `"<current> / <total>"` line progress.
* **Elapsed / ETA** (`sensor.redline_cnc_elapsed_eta`): Job timer (`"HH:MM:SS"`).
* **Work Position X / Y / Z / A** (`sensor.redline_cnc_work_position_x`, etc.): Work coordinates (`WPos = MPos - WCS`), automatically scaled to `"in"` or `"mm"`.
* **Machine Position X / Y / Z / A** (`sensor.redline_cnc_machine_position_x`, etc.): Raw machine coordinates (`MPos`).
* **Target Spindle Speed** (`sensor.redline_cnc_target_spindle_speed`): Commanded spindle speed in `rpm`.
* **Target Feed Rate** (`sensor.redline_cnc_target_feed_rate`): Commanded feed rate (`in/min` or `mm/min`).
* **Active Tool** (`sensor.redline_cnc_active_tool`): Active tool number and description from the controller's tool table.
* **Coordinate Units** & **RTS-X Version**: Diagnostic sensors.

### Binary Sensors (`binary_sensor`)
* **Controller Connected** (`binary_sensor.redline_cnc_controller_connected`): WebSocket connectivity status.
* **Job Running** (`binary_sensor.redline_cnc_job_running`): `on` whenever the machine is actively running a job.
* **E-Stop Active** (`binary_sensor.redline_cnc_e_stop_active`): `on` when the controller is in Emergency Stop.
* **IO Spindle** (`binary_sensor.redline_cnc_io_spindle`): `on` when spindle (`M3`/`M4`) is active during a job (or reported directly via WebSocket).
* **IO Vacuum / Flood Coolant (M8)** (`binary_sensor.redline_cnc_io_vacuum_flood_coolant_m8`): `on` when `M8` (or direct `vacuum`/`flood` IO) is active.
* **IO Air Assist / Mist Coolant (M7)** (`binary_sensor.redline_cnc_io_air_assist_mist_coolant_m7`): `on` when `M7` (or direct `air_assist`/`mist` IO) is active.
* **IO Aux Output P0 / P1 (M62/M64)** (`binary_sensor.redline_cnc_io_aux_output_p0_m62_m64`): Digital output pin states.

### Camera (`camera`)
* **Camera Feed** (`camera.redline_cnc_camera_feed`): Live `640x480` MJPEG stream and still snapshots decoded on-demand from the controller's WebSocket `image_data` stream.

### Button (`button`)
* **Refresh Status** (`button.redline_cnc_refresh_status`): Forces an immediate refresh of status, version, tool table, and file tree.

---

## Installation

### Option 1: HACS (Recommended)
1. Open **HACS** in Home Assistant -> **Integrations** -> top-right menu (`⋮`) -> **Custom repositories**.
2. Add this repository URL with category **Integration**.
3. Click **Download** on **Redline CNC (RTS-X)** and restart Home Assistant.
4. Go to **Settings** -> **Devices & Services** -> **Add Integration** -> search for **Redline CNC (RTS-X)**.
5. Enter your controller host (`192.168.60.63`) and WebSocket port (`8080`).

### Option 2: Manual Installation
1. Copy the `custom_components/redline_cnc` directory into your Home Assistant `<config>/custom_components/` directory.
2. Restart Home Assistant and add **Redline CNC (RTS-X)** from **Settings** -> **Devices & Services**.

---

## Example Home Assistant Automation (Shop Vacuum / Air Assist)

```yaml
automation:
  - alias: "CNC Shop Vacuum Auto Control"
    description: "Turn on shop vac when CNC spindle or job starts, turn off 10s after stop"
    trigger:
      - platform: state
        entity_id:
          - binary_sensor.redline_cnc_job_running
          - binary_sensor.redline_cnc_io_spindle
          - binary_sensor.redline_cnc_io_vacuum_flood_coolant_m8
    action:
      - choose:
          - conditions:
              - condition: or
                conditions:
                  - condition: state
                    entity_id: binary_sensor.redline_cnc_job_running
                    state: "on"
                  - condition: state
                    entity_id: binary_sensor.redline_cnc_io_spindle
                    state: "on"
                  - condition: state
                    entity_id: binary_sensor.redline_cnc_io_vacuum_flood_coolant_m8
                    state: "on"
            sequence:
              - service: switch.turn_on
                target:
                  entity_id: switch.shop_vacuum
        default:
          - delay: "00:00:10"
          - service: switch.turn_off
            target:
              entity_id: switch.shop_vacuum
```
