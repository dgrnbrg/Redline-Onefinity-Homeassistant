"""Unit tests for controller client TTL pruning, workflow version replacement, and byte-exact verification."""

from __future__ import annotations

import datetime
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from redline_job_linker.controller_client import ControllerWorkflowManager


def run_coro(coro):
    """Run an async coroutine synchronously without creating Windows IOCP / loopback sockets."""
    stack = [coro]
    val = None
    while stack:
        try:
            nxt = stack[-1].send(val)
            val = None
            if hasattr(nxt, "send"):
                stack.append(nxt)
        except StopIteration as e:
            stack.pop()
            val = e.value
    return val


class MockControllerWorkflowManager(ControllerWorkflowManager):
    """Mock subclass of ControllerWorkflowManager that stores files in memory."""

    def __init__(self, initial_files: list[dict]):
        super().__init__(host="127.0.0.1", port=8080)
        self.files_db = list(initial_files)
        self.deleted_paths: list[str] = []

    async def get_workflow_files(self) -> list[dict]:
        return list(self.files_db)

    async def delete_file(self, file_path: str) -> bool:
        self.deleted_paths.append(file_path)
        fname = file_path.replace("gcode/workflow/", "")
        self.files_db = [f for f in self.files_db if f.get("name") != fname]
        return True


class MockWSClient:
    def __init__(self, host: str, port: int = 8080, timeout: float = 5.0, tree: dict | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_error = None
        self.tree = tree or {}
        self._queue: list[dict] = []

    async def connect(self):
        return True

    async def send_json(self, data: dict):
        if data.get("command") == "refreshFileList":
            self._queue.append(self.tree)
        return True

    async def read_json(self, timeout: float = 2.0):
        return self._queue.pop(0) if self._queue else None

    async def drain_incoming(self, timeout: float = 0.05):
        return []

    async def close(self):
        pass


class TestControllerClient(unittest.TestCase):
    def test_ttl_stale_job_pruning(self):
        now = datetime.datetime.now()
        fresh_time = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        stale_time_1 = (now - datetime.timedelta(hours=26)).strftime("%Y-%m-%d %H:%M:%S")
        stale_time_2 = (now - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")

        mock_files = [
            {"name": "WF_bracket_G54_seq_20261108_1200.ngc", "modified": fresh_time, "size": 1000},
            {"name": "WF_old_job_G54_G55_20261106_1000.ngc", "modified": stale_time_1, "size": 2000},
            {"name": "WF_ancient_G54_20261101_0800.ngc", "modified": stale_time_2, "size": 3000},
        ]

        mgr = MockControllerWorkflowManager(mock_files)
        deleted = run_coro(mgr.prune_stale_jobs(ttl_hours=24.0))

        self.assertEqual(len(deleted), 2)
        self.assertIn("WF_old_job_G54_G55_20261106_1000.ngc", deleted)
        self.assertIn("WF_ancient_G54_20261101_0800.ngc", deleted)

        # Fresh file must remain untouched
        remaining_names = [f["name"] for f in mgr.files_db]
        self.assertIn("WF_bracket_G54_seq_20261108_1200.ngc", remaining_names)

    def test_delete_earlier_workflow_versions(self):
        mock_files = [
            {"name": "WF_bracket_plate_G54_G55_seq.ngc", "modified": "2026-11-08 10:00:00", "size": 1000},
            {"name": "WF_bracket_plate_G54_G55_bytool_aircarve.ngc", "modified": "2026-11-08 10:05:00", "size": 1200},
            {"name": "WF_unrelated_sign_G54_seq.ngc", "modified": "2026-11-08 10:10:00", "size": 1500},
        ]

        mgr = MockControllerWorkflowManager(mock_files)
        deleted = run_coro(
            mgr.delete_earlier_workflow_versions("WF_bracket_plate_G54_G55")
        )

        # Both previous iterations of WF_bracket_plate_G54_G55 must be deleted
        self.assertEqual(len(deleted), 2)
        self.assertIn("WF_bracket_plate_G54_G55_seq.ngc", deleted)
        self.assertIn("WF_bracket_plate_G54_G55_bytool_aircarve.ngc", deleted)

        # Unrelated job must NOT be deleted
        remaining_names = [f["name"] for f in mgr.files_db]
        self.assertIn("WF_unrelated_sign_G54_seq.ngc", remaining_names)

    def test_execute_sync_exact_byte_verification_success(self):
        content = "G0 X0 Y0 Z5\nM30\n"
        expected_len = len(content.encode("utf-8"))  # 16 bytes

        tree = {
            "name": "gcode",
            "children": [
                {
                    "name": "workflow",
                    "children": [
                        {"name": "Bracket.ngc", "size": expected_len, "modified": "2026-11-12 12:00:00"}
                    ],
                }
            ],
        }

        async def noop_sleep(*a, **kw):
            pass

        mgr = ControllerWorkflowManager(host="127.0.0.1", port=8080)
        with patch("redline_job_linker.controller_client.SimpleWebSocketClient", side_effect=lambda *a, **kw: MockWSClient(*a, tree=tree, **kw)):
            with patch("asyncio.sleep", side_effect=noop_sleep):
                res = run_coro(mgr.execute_sync(files={"Bracket.ngc": content}))

        self.assertTrue(res["ok"])
        self.assertEqual(res["verified"], ["Bracket.ngc"])
        self.assertEqual(res["uploaded"], ["Bracket.ngc"])

    def test_execute_sync_exact_byte_verification_rejects_zero_or_mismatched_bytes(self):
        content = "G0 X0 Y0 Z5\nM30\n"
        expected_len = len(content.encode("utf-8"))  # 16 bytes

        # 1. Controller reports 0 bytes
        tree_zero = {
            "name": "gcode",
            "children": [
                {
                    "name": "workflow",
                    "children": [
                        {"name": "Bracket.ngc", "size": 0, "modified": "2026-11-12 12:00:00"}
                    ],
                }
            ],
        }

        async def noop_sleep(*a, **kw):
            pass

        mgr = ControllerWorkflowManager(host="127.0.0.1", port=8080)
        with patch("redline_job_linker.controller_client.SimpleWebSocketClient", side_effect=lambda *a, **kw: MockWSClient(*a, tree=tree_zero, **kw)):
            with patch("asyncio.sleep", side_effect=noop_sleep):
                res_zero = run_coro(mgr.execute_sync(files={"Bracket.ngc": content}))

        self.assertFalse(res_zero["ok"])
        self.assertEqual(res_zero["verified"], [])
        self.assertIn(("Bracket.ngc", 0, expected_len), res_zero["size_mismatches"])
        self.assertIn("size mismatch", res_zero["error"])

        # 2. Controller reports truncated byte count (e.g. 10 of 16 bytes)
        tree_partial = {
            "name": "gcode",
            "children": [
                {
                    "name": "workflow",
                    "children": [
                        {"name": "Bracket.ngc", "size": 10, "modified": "2026-11-12 12:00:00"}
                    ],
                }
            ],
        }

        with patch("redline_job_linker.controller_client.SimpleWebSocketClient", side_effect=lambda *a, **kw: MockWSClient(*a, tree=tree_partial, **kw)):
            with patch("asyncio.sleep", side_effect=noop_sleep):
                res_partial = run_coro(mgr.execute_sync(files={"Bracket.ngc": content}))

        self.assertFalse(res_partial["ok"])
        self.assertEqual(res_partial["verified"], [])
        self.assertIn(("Bracket.ngc", 10, expected_len), res_partial["size_mismatches"])

    def test_execute_sync_rejects_missing_file(self):
        content = "G0 X0 Y0 Z5\nM30\n"

        # Controller folder is empty
        tree_empty = {
            "name": "gcode",
            "children": [
                {
                    "name": "workflow",
                    "children": [],
                }
            ],
        }

        async def noop_sleep(*a, **kw):
            pass

        mgr = ControllerWorkflowManager(host="127.0.0.1", port=8080)
        with patch("redline_job_linker.controller_client.SimpleWebSocketClient", side_effect=lambda *a, **kw: MockWSClient(*a, tree=tree_empty, **kw)):
            with patch("asyncio.sleep", side_effect=noop_sleep):
                res_empty = run_coro(mgr.execute_sync(files={"Bracket.ngc": content}))

        self.assertFalse(res_empty["ok"])
        self.assertEqual(res_empty["verified"], [])
        self.assertEqual(res_empty["missing_files"], ["Bracket.ngc"])
        self.assertIn("files missing on controller", res_empty["error"])


if __name__ == "__main__":
    unittest.main()
