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


    def test_watcher_recursive_detection(self):
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
                # 1. Add file in nested subdirectory
                nested_dir = os.path.join(tmpdir, "subdir", "nested")
                os.makedirs(nested_dir, exist_ok=True)
                test_file = os.path.join(nested_dir, "nested_job.ngc")
                with open(test_file, "w") as f:
                    f.write("G0 X0 Y0\nM30\n")

                time.sleep(0.4)
                ready = watcher.get_ready_files()
                self.assertIn(os.path.abspath(test_file), ready)

                # 2. Add second file in another subfolder
                subfolder2 = os.path.join(tmpdir, "other_folder")
                os.makedirs(subfolder2, exist_ok=True)
                test_file2 = os.path.join(subfolder2, "job2.tap")
                with open(test_file2, "w") as f:
                    f.write("G1 X10 Y10\nM30\n")

                time.sleep(0.4)
                ready2 = watcher.get_ready_files()
                self.assertEqual(len(ready2), 2)
                self.assertIn(os.path.abspath(test_file2), ready2)

                # 3. Delete nested file
                os.remove(test_file)
                time.sleep(0.4)
                ready3 = watcher.get_ready_files()
                self.assertEqual(len(ready3), 1)
                self.assertNotIn(os.path.abspath(test_file), ready3)
                self.assertIn(os.path.abspath(test_file2), ready3)
            finally:
                watcher.stop()


    def test_watcher_natural_sort_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events: list[list[str]] = []

            watcher = DirectoryWatcher(
                watch_dir=tmpdir,
                on_change_callback=lambda files: events.append(list(files)),
                poll_interval=0.1,
                debounce_seconds=0.1,
            )
            watcher.start()

            try:
                # Add files where alphabetical vs natural sort differs
                # e.g. Job_10 would precede Job_2 in naive alphabetical sort
                f10 = os.path.join(tmpdir, "Job_10.ngc")
                f2 = os.path.join(tmpdir, "Job_2.ngc")
                f1 = os.path.join(tmpdir, "Job_1.ngc")
                for path in (f10, f2, f1):
                    with open(path, "w") as f:
                        f.write("G0 X0\nM30\n")

                time.sleep(0.4)
                ready = watcher.get_ready_files()
                basenames = [os.path.basename(p) for p in ready]
                self.assertEqual(basenames, ["Job_1.ngc", "Job_2.ngc", "Job_10.ngc"])
            finally:
                watcher.stop()


if __name__ == "__main__":
    unittest.main()
