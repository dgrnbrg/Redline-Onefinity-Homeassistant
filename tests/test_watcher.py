"""Unit tests for DirectoryWatcher with debounced write detection."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from redline_job_linker.watcher import DirectoryWatcher


class TestDirectoryWatcher(unittest.TestCase):
    def test_watcher_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events: list[list[str]] = []

            def on_change(files: list[str]) -> None:
                events.append(list(files))

            watcher = DirectoryWatcher(
                watch_dir=tmpdir,
                on_change_callback=on_change,
                poll_interval=0.1,
                debounce_seconds=0.1,
            )
            watcher.start()

            try:
                # 1. Add file
                test_file = os.path.join(tmpdir, "job1.ngc")
                with open(test_file, "w") as f:
                    f.write("G0 X0 Y0\nM30\n")

                # Wait for poll cycles to stabilize
                time.sleep(0.4)

                ready = watcher.get_ready_files()
                self.assertIn(test_file, ready)

                # 2. Add second file
                test_file2 = os.path.join(tmpdir, "job2.gcode")
                with open(test_file2, "w") as f:
                    f.write("G1 X10 Y10\nM30\n")

                time.sleep(0.4)
                ready2 = watcher.get_ready_files()
                self.assertEqual(len(ready2), 2)

                # 3. Delete file
                os.remove(test_file)
                time.sleep(0.4)
                ready3 = watcher.get_ready_files()
                self.assertEqual(len(ready3), 1)
                self.assertNotIn(test_file, ready3)
            finally:
                watcher.stop()


if __name__ == "__main__":
    unittest.main()
