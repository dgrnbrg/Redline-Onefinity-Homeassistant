"""In-memory integration tests for web server endpoints."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest

from redline_job_linker.config import JobLinkerConfig
from redline_job_linker.server import JobLinkerRequestHandler, JobLinkerServerContext

SAMPLE_NGC = """(Fusion 360)
T1 M6
G0 X0 Y0 Z5.0
M3 S18000
G1 Z-2.0 F600
G1 X20.0 Y20.0
G0 Z5.0
M5
M30
"""


class MockRequestSocket:
    """In-memory socket simulation for testing HTTP handler without network syscalls."""

    def __init__(self, raw_input: bytes):
        self.rfile = io.BytesIO(raw_input)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        if "b" in mode and "r" in mode:
            return self.rfile
        return self.wfile

    def sendall(self, data: bytes):
        self.wfile.write(data)


class TestServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.watched = os.path.join(cls.tmpdir.name, "cam_watched")
        os.makedirs(cls.watched, exist_ok=True)

        sample_path = os.path.join(cls.watched, "Bracket.ngc")
        with open(sample_path, "w") as f:
            f.write(SAMPLE_NGC)

        cls.config = JobLinkerConfig(
            watched_dir=cls.watched,
            controller_host="127.0.0.1",
            controller_port=8080,
            server_port=8088,
            auto_upload_staged_enabled=False,
        )

        cls.context = JobLinkerServerContext(cls.config)

    @classmethod
    def tearDownClass(cls):
        if cls.context.watcher:
            cls.context.watcher.stop()
        cls.tmpdir.cleanup()

    def _execute_http(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        req_lines = [
            f"{method} {path} HTTP/1.0",
            "Host: localhost",
            "Connection: close",
            f"Content-Length: {len(body_bytes)}",
            "Content-Type: application/json",
            "",
            "",
        ]
        raw_req = "\r\n".join(req_lines).encode("utf-8") + body_bytes
        mock_sock = MockRequestSocket(raw_req)

        class BoundHandler(JobLinkerRequestHandler):
            def __init__(handler_self, req, client, srv):
                handler_self.context = self.context
                super().__init__(req, client, srv)

        BoundHandler(mock_sock, ("127.0.0.1", 12345), None)

        output_bytes = mock_sock.wfile.getvalue()
        parts = output_bytes.split(b"\r\n\r\n", 1)
        header_text = parts[0].decode("utf-8", "ignore")
        body_text = parts[1].decode("utf-8", "ignore") if len(parts) > 1 else ""

        status_line = header_text.splitlines()[0]
        status_code = int(status_line.split(" ")[1])

        try:
            parsed_json = json.loads(body_text)
            return status_code, parsed_json
        except Exception:
            return status_code, body_text

    def test_static_index_html(self):
        status, html = self._execute_http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Redline Workflow Linker", html)

    def test_get_config(self):
        status, cfg = self._execute_http("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["watched_dir"], self.watched)

    def test_get_staged_jobs(self):
        status, res = self._execute_http("GET", "/api/staged")
        self.assertEqual(status, 200)
        self.assertTrue(len(res["jobs"]) >= 1)
        bracket = next(j for j in res["jobs"] if j["name"] == "Bracket")
        self.assertEqual(bracket["tools_used"], [1])
        self.assertIn("stale_ttl_hours", res)
        self.assertIn("mtime", bracket)
        self.assertGreater(bracket["mtime"], 0)

    def test_staged_jobs_chronological_and_alphabetical_sorting(self):
        # Create file Zebra (older timestamp) and file Alpha (newer timestamp)
        path_z = os.path.join(self.watched, "Zebra.ngc")
        path_a = os.path.join(self.watched, "Alpha.ngc")
        with open(path_z, "w") as f:
            f.write(SAMPLE_NGC)
        with open(path_a, "w") as f:
            f.write(SAMPLE_NGC)
        now = time.time()
        os.utime(path_z, (now - 500, now - 500))
        os.utime(path_a, (now - 50, now - 50))
        self.context.scan_staged_jobs()

        status, res = self._execute_http("GET", "/api/staged")
        self.assertEqual(status, 200)
        jobs = res["jobs"]

        # Chronological sort (newest first) puts Alpha before Zebra
        chrono_sorted = sorted(jobs, key=lambda j: j.get("mtime", 0), reverse=True)
        alpha_idx = next(i for i, j in enumerate(chrono_sorted) if j["name"] == "Alpha")
        zebra_idx = next(i for i, j in enumerate(chrono_sorted) if j["name"] == "Zebra")
        self.assertLess(alpha_idx, zebra_idx)

        # Alphabetical sort (A-Z) puts Alpha before Zebra
        alpha_sorted = sorted(jobs, key=lambda j: j.get("name", ""))
        alpha_idx_2 = next(i for i, j in enumerate(alpha_sorted) if j["name"] == "Alpha")
        zebra_idx_2 = next(i for i, j in enumerate(alpha_sorted) if j["name"] == "Zebra")
        self.assertLess(alpha_idx_2, zebra_idx_2)

    def test_get_job_detail_geometry(self):
        status, res = self._execute_http("GET", "/api/job/job_bracket")
        self.assertEqual(status, 200)
        self.assertEqual(res["name"], "Bracket")
        self.assertIn("geometry", res)
        self.assertTrue(len(res["geometry"]) > 0)
        self.assertEqual(res["geometry"][0]["type"], "rapid")

    def test_assemble_endpoint(self):
        payload = {
            "slots": [
                {
                    "slot_id": "slot_1",
                    "job_id": "job_bracket",
                    "wcs": "G54",
                    "z_shift": 1.0,
                    "is_air_carve": False,
                    "pauses": [
                        {
                            "line": 5,
                            "type": "M0",
                            "msg": "Inspect fixture",
                            "retract": "safe_z",
                        }
                    ],
                }
            ],
            "strategy": "part_by_part",
        }
        status, res = self._execute_http("POST", "/api/assemble", body=payload)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIn("WF_bracket_G54", res["workflow_key"])
        self.assertIn("files", res)
        primary = res["primary_filename"]
        content = res["files"][primary]
        self.assertIn("G54           ; Switch to fixture coordinate system", content)
        self.assertIn("M0 (MSG, Inspect fixture)", content)

    def test_assemble_endpoint_cleared_strategy(self):
        # When strategy is cleared to 'none' in manual sequencing mode
        payload = {
            "slots": [
                {
                    "slot_id": "slot_1",
                    "job_id": "job_bracket",
                    "wcs": "G54",
                    "z_shift": 0.0,
                }
            ],
            "strategy": "none",
        }
        status, res = self._execute_http("POST", "/api/assemble", body=payload)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        primary = res["primary_filename"]
        content = res["files"][primary]
        self.assertIn("G54           ; Switch to fixture coordinate system", content)


    def test_staged_jobs_recursive_scan(self):
        nested_dir = os.path.join(self.watched, "subfolder", "project_x")
        os.makedirs(nested_dir, exist_ok=True)
        nested_file = os.path.join(nested_dir, "Widget.ngc")
        with open(nested_file, "w") as f:
            f.write(SAMPLE_NGC)

        try:
            status, res = self._execute_http("GET", "/api/staged")
            self.assertEqual(status, 200)
            job_names = [j["name"] for j in res["jobs"]]
            self.assertIn("Widget", job_names)
        finally:
            if os.path.exists(nested_file):
                os.remove(nested_file)
            try:
                os.removedirs(nested_dir)
            except OSError:
                pass

    def test_get_job_gcode(self):
        status, res = self._execute_http("GET", "/api/job/job_bracket/gcode")
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["job_id"], "job_bracket")
        self.assertIn("gcode", res)
        self.assertIn("T1 M6", res["gcode"])

    def test_parse_gcode_endpoint(self):
        sample_code = """(Fusion 360)
G20
T2 M6
G0 X1.0 Y1.0 Z0.5
G1 Z-0.1 F20.0
G1 X2.0 Y1.0
G0 Z0.5
M30
"""
        status, res = self._execute_http("POST", "/api/parse_gcode", body={"gcode": sample_code})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["units"], "imperial")
        self.assertEqual(res["unit_suffix"], "in")
        self.assertEqual(res["tools_used"], [2])
        self.assertGreater(len(res["geometry"]), 0)
        self.assertGreater(len(res["operations"]), 0)


    def test_sync_logs_endpoint(self):
        status, res = self._execute_http("GET", "/api/sync/logs")
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIn("history", res)
        self.assertIn("host", res)

    def test_controller_test_endpoint(self):
        def mock_test(*args, **kwargs):
            return {"ok": True, "host": "127.0.0.1", "port": 8080, "latency_ms": 1.2, "error": None}

        orig = self.context.controller_mgr.test_connection_detailed
        self.context.controller_mgr.test_connection_detailed = mock_test
        try:
            status, res = self._execute_http("GET", "/api/controller/test")
            self.assertEqual(status, 200)
            self.assertTrue(res["ok"])
            self.assertEqual(res["latency_ms"], 1.2)
        finally:
            self.context.controller_mgr.test_connection_detailed = orig

    def test_sync_endpoint(self):
        def mock_sync(*args, **kwargs):
            files = kwargs.get("files") or (args[0] if args else {})
            wf = kwargs.get("workflow_key") or (args[1] if len(args) > 1 else "WF_test")
            return {
                "ok": True,
                "workflow_key": wf,
                "uploaded": list(files.keys()),
                "replaced_earlier": [],
                "pruned_stale": [],
                "logs": ["[12:00:00] Mock sync success"],
                "duration_sec": 0.05,
            }

        orig = self.context.controller_mgr.execute_sync
        self.context.controller_mgr.execute_sync = mock_sync
        try:
            payload = {
                "slots": [
                    {
                        "slot_id": "slot_1",
                        "job_id": "job_bracket",
                        "wcs": "G54",
                        "z_shift": 0.0,
                        "is_air_carve": False,
                    }
                ],
                "strategy": "part_by_part",
            }
            status, res = self._execute_http("POST", "/api/sync", body=payload)
            self.assertEqual(status, 200)
            self.assertTrue(res["ok"])
            self.assertIn("bracket", res["uploaded"][0].lower())
            self.assertTrue(len(self.context.sync_history) > 0)
        finally:
            self.context.controller_mgr.execute_sync = orig

    def test_job_safe_z_override_endpoint(self):
        status, res = self._execute_http("POST", "/api/job/job_bracket/safe_z", body={"safe_z": 3.5})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["safe_z_threshold"], 3.5)
        self.assertIn("operations", res)

    def test_job_manual_splits_endpoint(self):
        status, res = self._execute_http(
            "POST",
            "/api/job/job_bracket/safe_z",
            body={"safe_z": 3.5, "manual_splits": [3]}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIn("operations", res)
        # Should contain an op starting at line 3
        starts = [op["start_line"] for op in res["operations"]]
        self.assertIn(3, starts)

    def test_job_auto_reset_endpoint(self):
        # Override first
        self._execute_http(
            "POST",
            "/api/job/job_bracket/safe_z",
            body={"safe_z": 1.0, "manual_splits": [3]}
        )
        # Now trigger Auto Reset
        status, res = self._execute_http(
            "POST",
            "/api/job/job_bracket/safe_z",
            body={"reset_to_inferred": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        # Should be reset to conservative threshold (>= 7.0 since job_bracket max rapid is 5.0mm)
        self.assertGreaterEqual(res["safe_z_threshold"], 7.0)
        self.assertEqual(len(res["operations"]), 1)
        self.assertEqual(res["operations"][0]["start_line"], 1)

    def test_surfacing_job_auto_reset_end_to_end(self):
        # Program simulating multi-pass surfacing with routine 5.0mm clearance retracts
        gcode = """G21
G0 X0 Y0 Z5.0
G1 Z-1.0 F300
G1 X10.0
G0 Z1.0
G0 X12.0
G1 Z-1.0
G1 X20.0
G0 Z5.0
G0 X22.0
G1 Z-1.0
G1 X30.0
G0 Z5.0
G0 X32.0
G1 Z-1.0
G1 X40.0
G0 Z5.0
M30
"""
        surf_path = os.path.join(self.watched, "Surfacing.ngc")
        with open(surf_path, "w") as f:
            f.write(gcode)
        self.context.scan_staged_jobs()
        job = self.context.get_job_by_id("job_surfacing")
        self.assertIsNotNone(job)

        # Inferred threshold must be above 5.0 (5.0 + 2.0 = 7.0) -> exactly 1 operation
        self.assertGreaterEqual(job.combined_parsed_job.safe_z_threshold, 7.0)
        self.assertEqual(len(job.combined_parsed_job.operations), 1)

        # 1. Override safe_z to 3.0 -> forces splits at every 5.0mm retract
        status, res = self._execute_http(
            "POST",
            "/api/job/job_surfacing/safe_z",
            body={"safe_z": 3.0}
        )
        self.assertEqual(status, 200)
        self.assertGreater(len(res["operations"]), 1)

        # 2. Trigger Auto Reset -> must revert to conservative threshold and 1 operation
        status, res = self._execute_http(
            "POST",
            "/api/job/job_surfacing/safe_z",
            body={"reset_to_inferred": True}
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(res["safe_z_threshold"], 7.0)
        self.assertEqual(len(res["operations"]), 1)

    def test_job_gcode_sliced_endpoint(self):
        status, res = self._execute_http("GET", "/api/job/job_bracket/gcode?start=2&end=4")
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIn("G0 X0 Y0 Z5.0", res["gcode"])

    def test_assemble_multi_op_slots(self):
        payload = {
            "slots": [
                {
                    "slot_id": "slot_1",
                    "job_id": "job_bracket",
                    "op_index": 1,
                    "op_name": "Roughing",
                    "start_line": 1,
                    "end_line": 5,
                    "wcs": "G54",
                    "z_shift": 0.0,
                    "is_air_carve": False,
                },
                {
                    "slot_id": "slot_2",
                    "job_id": "job_bracket",
                    "op_index": 2,
                    "op_name": "Finishing",
                    "start_line": 6,
                    "end_line": 9,
                    "wcs": "G55",
                    "z_shift": 0.0,
                    "is_air_carve": False,
                },
            ],
            "strategy": "part_by_part",
        }
        status, res = self._execute_http("POST", "/api/assemble", body=payload)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        primary = res["primary_filename"]
        content = res["files"][primary]
        self.assertIn("G54           ; Switch to fixture coordinate system", content)
        self.assertIn("G55           ; Switch to fixture coordinate system", content)

    def test_auto_upload_staged_files_with_expiry(self):
        fresh_path = os.path.join(self.watched, "FreshJob.ngc")
        with open(fresh_path, "w") as f:
            f.write("G0 X0 Y0 Z10\nM30\n")

        expired_path = os.path.join(self.watched, "ExpiredJob.ngc")
        with open(expired_path, "w") as f:
            f.write("G0 X5 Y5 Z10\nM30\n")

        # Set expired file's mtime to 48 hours ago (> 24h TTL)
        past_time = time.time() - (48 * 3600)
        os.utime(expired_path, (past_time, past_time))

        self.context.scan_staged_jobs()

        mock_sync_calls = []

        def mock_execute_sync(*args, **kwargs):
            files = kwargs.get("files") or (args[0] if args else {})
            workflow_key = kwargs.get("workflow_key") or (args[1] if len(args) > 1 else "")
            ttl_hours = kwargs.get("ttl_hours") or (args[2] if len(args) > 2 else 24.0)
            subfolder = kwargs.get("subfolder") or (args[3] if len(args) > 3 else "gcode/workflow")
            mock_sync_calls.append({
                "files": dict(files),
                "workflow_key": workflow_key,
                "ttl_hours": ttl_hours,
                "subfolder": subfolder,
            })
            return {
                "ok": True,
                "workflow_key": workflow_key,
                "uploaded": list(files.keys()),
                "verified": list(files.keys()),
                "replaced_earlier": [],
                "pruned_stale": [],
                "logs": [],
                "duration_sec": 0.05,
            }

        orig_execute_sync = self.context.controller_mgr.execute_sync
        self.context.controller_mgr.execute_sync = mock_execute_sync

        try:
            # 1. Trigger staged sync
            res = self.context.sync_staged_files_now()
            self.assertIsNotNone(res)
            self.assertTrue(res["ok"])

            # Verify only non-expired files uploaded with their exact filenames
            self.assertEqual(len(mock_sync_calls), 1)
            uploaded_files = mock_sync_calls[0]["files"]
            self.assertIn("FreshJob.ngc", uploaded_files)
            self.assertNotIn("ExpiredJob.ngc", uploaded_files)
            self.assertEqual(mock_sync_calls[0]["workflow_key"], "")

            # 2. Subsequent sync without changes should be a no-op
            mock_sync_calls.clear()
            res_noop = self.context.sync_staged_files_now()
            self.assertIsNone(res_noop)
            self.assertEqual(len(mock_sync_calls), 0)

            # 3. Touch/modify FreshJob.ngc -> should trigger re-upload
            with open(fresh_path, "w") as f:
                f.write("G0 X10 Y10 Z10\nM30\n")
            new_time = time.time() + 2
            os.utime(fresh_path, (new_time, new_time))

            res_modified = self.context.sync_staged_files_now()
            self.assertIsNotNone(res_modified)
            self.assertEqual(len(mock_sync_calls), 1)
            self.assertIn("FreshJob.ngc", mock_sync_calls[0]["files"])

            # 4. Test POST /api/sync/staged endpoint
            status, post_res = self._execute_http("POST", "/api/sync/staged")
            self.assertEqual(status, 200)
            self.assertTrue(post_res["ok"])
        finally:
            self.context.controller_mgr.execute_sync = orig_execute_sync
            if os.path.exists(fresh_path):
                os.remove(fresh_path)
            if os.path.exists(expired_path):
                os.remove(expired_path)
            self.context.scan_staged_jobs()


if __name__ == "__main__":
    unittest.main()
