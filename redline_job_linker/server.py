"""Lightweight standard-library HTTP server providing the REST API and web UI."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import mimetypes
import os
from pathlib import Path
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import JobLinkerConfig, load_config, save_config
from .controller_client import ControllerWorkflowManager
from .engine.assembler import AssemblyOutput, FixtureSlotConfig, assemble_workflow
from .engine.job_grouper import LogicalJob, group_files_into_logical_jobs
from .engine.parser import parse_gcode
from .engine.pause_manager import PausePoint
from .watcher import DirectoryWatcher, SUPPORTED_EXTENSIONS, natural_sort_key

_LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


def _run_async(coro_or_val: Any) -> Any:
    """Run an async coroutine synchronously or return val directly if already synchronous."""
    if asyncio.iscoroutine(coro_or_val):
        return asyncio.run(coro_or_val)
    return coro_or_val


class JobLinkerServerContext:
    """Shared state for the web server."""

    def __init__(self, cfg: JobLinkerConfig) -> None:
        self.config = cfg
        self.staged_jobs: list[LogicalJob] = []
        self.sync_history: list[dict[str, Any]] = []
        self.watcher: DirectoryWatcher | None = None
        self.controller_mgr = ControllerWorkflowManager(
            host=cfg.controller_host,
            port=cfg.controller_port,
        )
        self.init_watcher()

    def init_watcher(self) -> None:
        if self.watcher:
            self.watcher.stop()
        if self.config.watched_dir and os.path.isdir(self.config.watched_dir):
            self.watcher = DirectoryWatcher(
                watch_dir=self.config.watched_dir,
                on_change_callback=self.on_files_changed,
            )
            self.watcher.start()
            self.scan_staged_jobs()
        else:
            self.watcher = None
            self.staged_jobs = []

    def on_files_changed(self, ready_files: list[str]) -> None:
        self.scan_staged_jobs(ready_files)

    def scan_staged_jobs(self, file_paths: list[str] | None = None) -> None:
        if file_paths is None:
            if not self.config.watched_dir or not os.path.isdir(self.config.watched_dir):
                self.staged_jobs = []
                return
            entries: list[str] = []
            try:
                for root, _, files in os.walk(self.config.watched_dir):
                    for f in files:
                        if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS:
                            entries.append(os.path.abspath(os.path.join(root, f)))
            except OSError:
                entries = []
            file_paths = sorted(entries, key=natural_sort_key)

        self.staged_jobs = group_files_into_logical_jobs(
            file_paths,
            spoilboard_max_cut_depth_mm=self.config.spoilboard_max_cut_depth_mm,
        )
        _LOGGER.info("Staged %d logical jobs", len(self.staged_jobs))

    def get_job_by_id(self, job_id: str) -> LogicalJob | None:
        for j in self.staged_jobs:
            if j.id == job_id:
                return j
        return None


class JobLinkerRequestHandler(BaseHTTPRequestHandler):
    """Handles REST API and static UI requests."""

    context: JobLinkerServerContext  # Injected by server setup

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            self._send_json(self.context.config.to_dict())
            return

        if path == "/api/staged":
            self.context.scan_staged_jobs()
            summaries = [j.to_summary_dict() for j in self.context.staged_jobs]
            self._send_json({"jobs": summaries, "watched_dir": self.context.config.watched_dir, "stale_ttl_hours": self.context.config.stale_ttl_hours})
            return

        if path.startswith("/api/job/"):
            subpath = path.replace("/api/job/", "")
            if subpath.endswith("/gcode"):
                job_id = subpath.replace("/gcode", "")
                job = self.context.get_job_by_id(job_id)
                if not job:
                    self._send_error(404, f"Job {job_id} not found")
                    return
                query_params = urllib.parse.parse_qs(parsed.query)
                op_param = query_params.get("op", [None])[0]
                start_param = query_params.get("start", [None])[0]
                end_param = query_params.get("end", [None])[0]

                if job.is_multi_file and op_param is not None and op_param.isdigit():
                    op_idx = int(op_param)
                    if 1 <= op_idx <= len(job.subfiles):
                        sf = job.subfiles[op_idx - 1]
                        try:
                            with open(sf.filepath, "r", encoding="utf-8", errors="replace") as f:
                                op_gcode = f.read()
                        except OSError:
                            op_gcode = "\n".join(ln.raw_text for ln in sf.parsed.lines)
                        self._send_json({"ok": True, "job_id": job_id, "name": f"{job.name} - {sf.operation_name}", "gcode": op_gcode})
                        return

                if start_param is not None and end_param is not None:
                    try:
                        s_line = int(start_param)
                        e_line = int(end_param)
                        sliced_lines = [
                            ln.raw_text
                            for ln in job.combined_parsed_job.lines
                            if s_line <= ln.line_number <= e_line
                        ]
                        self._send_json({"ok": True, "job_id": job_id, "name": job.name, "gcode": "\n".join(sliced_lines)})
                        return
                    except (ValueError, TypeError):
                        pass

                # Retrieve raw gcode text from files or parsed lines
                raw_parts = []
                for sf in job.subfiles:
                    try:
                        with open(sf.filepath, "r", encoding="utf-8", errors="replace") as f:
                            raw_parts.append(f.read())
                    except OSError:
                        raw_parts.append("\n".join(ln.raw_text for ln in sf.parsed.lines))
                raw_gcode = "\n".join(raw_parts) if raw_parts else "\n".join(ln.raw_text for ln in job.combined_parsed_job.lines)
                self._send_json({"ok": True, "job_id": job_id, "name": job.name, "gcode": raw_gcode})
                return

            job_id = subpath
            job = self.context.get_job_by_id(job_id)
            if not job:
                self._send_error(404, f"Job {job_id} not found")
                return
            # Return detailed job info including geometry points
            data = job.to_summary_dict()
            data["geometry"] = [
                {
                    "x": pt.x,
                    "y": pt.y,
                    "z": pt.z,
                    "type": pt.motion_type,
                    "line": pt.line_number,
                    "tool": pt.tool,
                    "feed": pt.feed,
                }
                for pt in job.combined_parsed_job.toolpath_geometry
            ]
            data["tool_sections"] = [
                {
                    "tool": sec.tool_number,
                    "desc": sec.description,
                    "start": sec.start_line,
                    "end": sec.end_line,
                }
                for sec in job.combined_parsed_job.tool_sections
            ]
            self._send_json(data)
            return

        if path == "/api/controller/status":
            try:
                ok = _run_async(self.context.controller_mgr.test_connection())
                if self.context.controller_mgr.port != self.context.config.controller_port:
                    self.context.config.controller_port = self.context.controller_mgr.port
                    save_config(self.context.config)
                files = []
                if ok:
                    files = _run_async(self.context.controller_mgr.get_workflow_files())
                self._send_json({"connected": ok, "files": files, "folder": self.context.config.controller_subfolder, "port": self.context.config.controller_port})
            except Exception as err:
                self._send_json({"connected": False, "files": [], "error": str(err)})
            return

        if path == "/api/controller/test":
            try:
                diag = _run_async(self.context.controller_mgr.test_connection_detailed())
                if self.context.controller_mgr.port != self.context.config.controller_port:
                    self.context.config.controller_port = self.context.controller_mgr.port
                    save_config(self.context.config)
                self._send_json(diag)
            except Exception as err:
                self._send_json({"ok": False, "error": str(err)})
            return

        if path == "/api/sync/logs":
            self._send_json(
                {
                    "ok": True,
                    "host": self.context.config.controller_host,
                    "port": self.context.config.controller_port,
                    "history": self.context.sync_history,
                }
            )
            return

        # Serve static web frontend
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/parse_gcode":
            try:
                gcode_text = body.get("gcode", "")
                fname = body.get("filename", "custom.ngc")
                parsed = parse_gcode(gcode_text, filename=fname)
                self._send_json(
                    {
                        "ok": True,
                        "total_lines": parsed.total_lines,
                        "units": parsed.units,
                        "unit_suffix": "in" if parsed.units == "imperial" else "mm",
                        "tools_used": parsed.tools_used,
                        "safe_z_threshold": parsed.safe_z_threshold,
                        "extents": {
                            "min_x": parsed.min_x,
                            "max_x": parsed.max_x,
                            "min_y": parsed.min_y,
                            "max_y": parsed.max_y,
                            "min_z": parsed.min_z,
                            "max_z": parsed.max_z,
                            "min_cutting_z": parsed.min_cutting_z,
                            "max_cutting_z": parsed.max_cutting_z,
                        },
                        "operations": [
                            {
                                "op": op.op_number,
                                "name": op.name,
                                "tool": op.tool_number,
                                "start_line": op.start_line,
                                "end_line": op.end_line,
                                "min_z": op.min_z,
                                "max_z": op.max_z,
                                "min_cutting_z": op.min_cutting_z,
                                "max_cutting_z": op.max_cutting_z,
                                "lines": op.total_lines,
                            }
                            for op in parsed.operations
                        ],
                        "geometry": [
                            {
                                "x": pt.x,
                                "y": pt.y,
                                "z": pt.z,
                                "type": pt.motion_type,
                                "line": pt.line_number,
                                "tool": pt.tool,
                                "feed": pt.feed,
                            }
                            for pt in parsed.toolpath_geometry
                        ],
                        "tool_sections": [
                            {
                                "tool": sec.tool_number,
                                "desc": sec.description,
                                "start": sec.start_line,
                                "end": sec.end_line,
                            }
                            for sec in parsed.tool_sections
                        ],
                    }
                )
            except Exception as err:
                self._send_error(400, f"Parse failed: {err}")
            return

        if path.startswith("/api/job/") and path.endswith("/safe_z"):
            job_id = path.replace("/api/job/", "").replace("/safe_z", "")
            job = self.context.get_job_by_id(job_id)
            if not job:
                self._send_error(404, f"Job {job_id} not found")
                return
            new_z = body.get("safe_z")
            manual_splits = body.get("manual_splits")
            reset_to_inferred = bool(body.get("reset_to_inferred", False))

            try:
                from .engine.parser import _infer_safe_z_threshold, _infer_operations
                if reset_to_inferred:
                    threshold = _infer_safe_z_threshold(
                        geometry=job.combined_parsed_job.toolpath_geometry,
                        units=job.combined_parsed_job.units,
                    )
                    job.combined_parsed_job.safe_z_threshold = threshold
                    job.combined_parsed_job.inferred_safe_z_threshold = threshold
                    manual_splits = []
                elif new_z is not None:
                    threshold = float(new_z)
                    job.combined_parsed_job.safe_z_threshold = threshold
                else:
                    threshold = job.combined_parsed_job.safe_z_threshold

                new_ops, _ = _infer_operations(
                    parsed_lines=job.combined_parsed_job.lines,
                    geometry=job.combined_parsed_job.toolpath_geometry,
                    units=job.combined_parsed_job.units,
                    tool_descriptions=job.combined_parsed_job.tool_descriptions,
                    safe_z_threshold=threshold,
                    manual_splits=manual_splits,
                )
                job.combined_parsed_job.operations = new_ops
                self._send_json({
                    "ok": True,
                    "job_id": job_id,
                    "safe_z_threshold": threshold,
                    "inferred_safe_z_threshold": getattr(job.combined_parsed_job, "inferred_safe_z_threshold", threshold),
                    "operations": [
                        {
                            "op": op.op_number,
                            "name": op.name,
                            "tool": op.tool_number,
                            "start_line": op.start_line,
                            "end_line": op.end_line,
                            "lines": op.total_lines,
                        }
                        for op in new_ops
                    ],
                })
                return
            except Exception as err:
                self._send_error(400, f"Failed updating safe Z: {err}")
                return

        if path == "/api/config":
            try:
                # Normalize controller host and port
                raw_host = str(body.get("controller_host", "")).strip()
                if "://" in raw_host:
                    raw_host = raw_host.split("://", 1)[1]
                if "/" in raw_host:
                    raw_host = raw_host.split("/", 1)[0]
                if ":" in raw_host:
                    h_parts = raw_host.split(":")
                    raw_host = h_parts[0]
                    try:
                        body["controller_port"] = int(h_parts[1])
                    except ValueError:
                        pass
                body["controller_host"] = raw_host

                # If port was mistakenly set to 80 (HTTP UI port), RTS-X WebSocket runs on 8080
                port_val = body.get("controller_port")
                if port_val == 80:
                    _LOGGER.info("Config controller_port was set to 80 (HTTP UI); defaulting RTS-X WebSocket port to 8080")
                    body["controller_port"] = 8080

                new_cfg = JobLinkerConfig.from_dict(body)
                save_config(new_cfg)
                old_watched_dir = self.context.config.watched_dir
                self.context.config = new_cfg
                self.context.controller_mgr.host = new_cfg.controller_host
                self.context.controller_mgr.port = new_cfg.controller_port
                if old_watched_dir != new_cfg.watched_dir:
                    self.context.init_watcher()
                self._send_json({"ok": True, "config": new_cfg.to_dict()})
            except Exception as err:
                self._send_error(400, f"Invalid config: {err}")
            return

        if path == "/api/assemble":
            try:
                output = self._assemble_from_body(body)
                self._send_json(
                    {
                        "ok": True,
                        "workflow_key": output.workflow_key,
                        "primary_filename": output.primary_filename,
                        "files": output.files,
                        "tools": output.total_tools,
                        "parts": output.total_parts,
                        "strategy": output.strategy,
                        "has_air_carve": output.has_air_carve,
                    }
                )
            except Exception as err:
                self._send_error(400, f"Assembly failed: {err}")
            return

        if path == "/api/controller/test":
            try:
                diag = _run_async(self.context.controller_mgr.test_connection_detailed())
                self._send_json(diag)
            except Exception as err:
                self._send_json({"ok": False, "error": str(err)})
            return

        if path == "/api/sync":
            try:
                output = self._assemble_from_body(body)
                result = _run_async(
                    self.context.controller_mgr.execute_sync(
                        files=output.files,
                        workflow_key=output.workflow_key,
                        ttl_hours=self.context.config.stale_ttl_hours,
                        subfolder=self.context.config.controller_subfolder,
                    )
                )
                history_entry = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    **result,
                }
                self.context.sync_history.insert(0, history_entry)
                if len(self.context.sync_history) > 50:
                    self.context.sync_history = self.context.sync_history[:50]

                self._send_json(result)
            except Exception as err:
                _LOGGER.exception("Sync execution failed")
                err_result = {
                    "ok": False,
                    "workflow_key": getattr(output, "workflow_key", "unknown") if "output" in locals() else "unknown",
                    "uploaded": [],
                    "replaced_earlier": [],
                    "pruned_stale": [],
                    "error": f"Internal sync failure: {err}",
                    "logs": [f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Exception: {err}"],
                }
                self.context.sync_history.insert(0, {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    **err_result,
                })
                self._send_json(err_result)
            return

        self._send_error(404, "Endpoint not found")

    def _assemble_from_body(self, body: dict[str, Any]) -> AssemblyOutput:
        slots_data = body.get("slots", [])
        strategy = body.get("strategy", "part_by_part")
        single_part = bool(body.get("single_part", False))

        fixture_slots: list[FixtureSlotConfig] = []
        for s in slots_data:
            job_id = s.get("job_id", "")
            job = self.context.get_job_by_id(job_id)
            if not job:
                continue

            pauses_data = s.get("pauses", [])
            pause_points = [
                PausePoint(
                    line_number=int(p.get("line", 1)),
                    pause_type=p.get("type", "M0"),
                    message=p.get("msg", ""),
                    retract_mode=p.get("retract", "safe_z"),
                    custom_park_x=float(p.get("park_x", self.context.config.gantry_park_x)),
                    custom_park_y=float(p.get("park_y", self.context.config.gantry_park_y)),
                )
                for p in pauses_data
            ]

            fixture_slots.append(
                FixtureSlotConfig(
                    slot_id=s.get("slot_id", f"slot_{len(fixture_slots) + 1}"),
                    job=job,
                    wcs=s.get("wcs", "G54"),
                    z_shift=float(s.get("z_shift", 0.0)),
                    is_air_carve=bool(s.get("is_air_carve", False)),
                    air_carve_height_mm=float(
                        s.get("air_carve_height_mm", self.context.config.air_carve_height_mm)
                    ),
                    vacuum_mode=s.get("vacuum_mode", "default"),
                    air_assist_mode=s.get("air_assist_mode", "default"),
                    pauses=pause_points,
                    custom_gcode=s.get("custom_gcode", None),
                    op_index=int(s["op_index"]) if s.get("op_index") is not None else None,
                    op_name=s.get("op_name", None),
                    start_line=int(s["start_line"]) if s.get("start_line") is not None else None,
                    end_line=int(s["end_line"]) if s.get("end_line") is not None else None,
                )
            )

        return assemble_workflow(
            slots=fixture_slots,
            strategy=strategy,
            single_part=single_part,
            safe_z_command=self.context.config.safe_z_command,
            custom_epilogue=self.context.config.custom_epilogue,
            spindle_dwell=self.context.config.spindle_dwell_seconds,
        )

    def _serve_static(self, req_path: str) -> None:
        if req_path in ("/", ""):
            req_path = "/index.html"
        rel_path = req_path.lstrip("/")
        full_path = (WEB_DIR / rel_path).resolve()

        # Security check: must reside inside WEB_DIR
        try:
            full_path.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._send_error(403, "Forbidden")
            return

        if not full_path.exists() or not full_path.is_file():
            self._send_error(404, "Not Found")
            return

        mime_type, _ = mimetypes.guess_type(str(full_path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        try:
            with open(full_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except Exception as err:
            self._send_error(500, f"Error reading file: {err}")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, code: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=code)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep console output clean
        _LOGGER.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)
