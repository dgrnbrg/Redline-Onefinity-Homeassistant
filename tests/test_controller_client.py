"""Unit tests for controller client TTL pruning and workflow version replacement."""

from __future__ import annotations

import asyncio
import datetime
import unittest

from redline_job_linker.controller_client import ControllerWorkflowManager


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

        loop = asyncio.new_event_loop()
        try:
            # TTL = 24 hours
            deleted = loop.run_until_complete(mgr.prune_stale_jobs(ttl_hours=24.0))
        finally:
            loop.close()

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

        loop = asyncio.new_event_loop()
        try:
            deleted = loop.run_until_complete(
                mgr.delete_earlier_workflow_versions("WF_bracket_plate_G54_G55")
            )
        finally:
            loop.close()

        # Both previous iterations of WF_bracket_plate_G54_G55 must be deleted
        self.assertEqual(len(deleted), 2)
        self.assertIn("WF_bracket_plate_G54_G55_seq.ngc", deleted)
        self.assertIn("WF_bracket_plate_G54_G55_bytool_aircarve.ngc", deleted)

        # Unrelated job must NOT be deleted
        remaining_names = [f["name"] for f in mgr.files_db]
        self.assertIn("WF_unrelated_sign_G54_seq.ngc", remaining_names)


if __name__ == "__main__":
    unittest.main()
