"""Lightweight standard-library HTTP server providing the REST API and web UI."""

from __future__ import annotations

import asyncio
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
from .engine.pause_manager import PausePoint
from .watcher import DirectoryWatcher

_LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class JobLinkerServerContext:
    """Shared state for the web server."""

    def __init__(self, cfg: JobLinkerConfig) -> None:
        self.config = cfg
        self.staged_jobs: list[LogicalJob] = []
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
            entries = []
            for ext in (".ngc", ".gcode", ".nc", ".tap"):
                entries.extend(Path(self.config.watched_dir).glob(f"*{ext}"))
            file_paths = [str(p) for p in entries]

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
            self._send_json({"jobs": summaries, "watched_dir": self.context.config.watched_dir})
            return

        if path.startswith("/api/job/"):
            job_id = path.replace("/api/job/", "")
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
            loop = asyncio.new_event_loop()
            try:
                ok = loop.run_until_complete(self.context.controller_mgr.test_connection())
                files = []
                if ok:
                    files = loop.run_until_complete(self.context.controller_mgr.get_workflow_files())
                self._send_json({"connected": ok, "files": files, "folder": self.context.config.controller_subfolder})
            finally:
                loop.close()
            return

        # Serve static web frontend
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/config":
            try:
                new_cfg = JobLinkerConfig.from_dict(body)
                save_config(new_cfg)
                self.context.config = new_cfg
                self.context.controller_mgr.host = new_cfg.controller_host
                self.context.controller_mgr.port = new_cfg.controller_port
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

        if path == "/api/sync":
            try:
                output = self._assemble_from_body(body)
                loop = asyncio.new_event_loop()
                try:
                    # 1. Ensure folder exists
                    await_ensure = loop.run_until_complete(
                        self.context.controller_mgr.ensure_folder(self.context.config.controller_subfolder)
                    )
                    # 2. Prune stale jobs (TTL)
                    pruned = loop.run_until_complete(
                        self.context.controller_mgr.prune_stale_jobs(self.context.config.stale_ttl_hours)
                    )
                    # 3. Delete earlier versions of this logical workflow
                    replaced = loop.run_until_complete(
                        self.context.controller_mgr.delete_earlier_workflow_versions(output.workflow_key)
                    )
                    # 4. Upload newly generated file(s)
                    uploaded_files: list[str] = []
                    for fname, content in output.files.items():
                        ok = loop.run_until_complete(self.context.controller_mgr.upload_gcode(fname, content))
                        if ok:
                            uploaded_files.append(fname)
                    self._send_json(
                        {
                            "ok": len(uploaded_files) > 0,
                            "workflow_key": output.workflow_key,
                            "uploaded": uploaded_files,
                            "replaced_earlier": replaced,
                            "pruned_stale": pruned,
                        }
                    )
                finally:
                    loop.close()
            except Exception as err:
                self._send_error(500, f"Sync to controller failed: {err}")
            return

        self._send_error(404, "Endpoint not found")

    def _assemble_from_body(self, body: dict[str, Any]) -> AssemblyOutput:
        slots_data = body.get("slots", [])
        strategy = body.get("strategy", "part_by_part")

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
                )
            )

        return assemble_workflow(
            slots=fixture_slots,
            strategy=strategy,
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
