"""In-memory integration tests for web server endpoints."""

from __future__ import annotations

import io
import json
import os
import tempfile
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
            f"{method} {path} HTTP/1.1",
            "Host: localhost",
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


if __name__ == "__main__":
    unittest.main()
