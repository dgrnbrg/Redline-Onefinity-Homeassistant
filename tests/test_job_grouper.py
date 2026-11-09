"""Unit tests for multi-file CAM job grouping into unified LogicalJobs."""

from __future__ import annotations

import os
import tempfile
import unittest

from redline_job_linker.engine.job_grouper import group_files_into_logical_jobs

PART1_ROUGH_GCODE = """(Fusion 360)
T1 M6
G0 Z10.0
M3 S16000
G1 Z-5.0 F600
G1 X100.0 Y50.0
G0 Z10.0
M30
"""

PART2_FINISH_GCODE = """(Fusion 360)
T2 M6
G0 Z10.0
M3 S20000
G1 Z-5.0 F400
G1 X100.0 Y50.0
G0 Z10.0
M30
"""

OTHER_JOB_GCODE = """(Vectric)
T1 M6
G0 Z5.0
G1 Z-2.0 F800
M30
"""


class TestJobGrouper(unittest.TestCase):
    def test_multi_file_grouping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = os.path.join(tmpdir, "Bracket_1_Roughing.ngc")
            file2 = os.path.join(tmpdir, "Bracket_2_Finishing.ngc")
            file3 = os.path.join(tmpdir, "Plate.ngc")

            with open(file1, "w") as f:
                f.write(PART1_ROUGH_GCODE)
            with open(file2, "w") as f:
                f.write(PART2_FINISH_GCODE)
            with open(file3, "w") as f:
                f.write(OTHER_JOB_GCODE)

            logical_jobs = group_files_into_logical_jobs([file1, file2, file3])

            self.assertEqual(len(logical_jobs), 2)

            bracket_job = next(j for j in logical_jobs if "bracket" in j.name.lower())
            plate_job = next(j for j in logical_jobs if "plate" in j.name.lower())

            # Bracket should be recognized as a multi-file job with 2 operations
            self.assertTrue(bracket_job.is_multi_file)
            self.assertEqual(len(bracket_job.subfiles), 2)
            self.assertEqual(bracket_job.tools_used, [1, 2])
            self.assertGreater(bracket_job.mtime, 0)
            self.assertGreater(bracket_job.subfiles[0].mtime, 0)
            self.assertGreater(bracket_job.to_summary_dict()['mtime'], 0)

            # Plate should be a single-file job
            self.assertFalse(plate_job.is_multi_file)
            self.assertEqual(len(plate_job.subfiles), 1)


    def test_job_natural_sorting_and_caching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_10 = os.path.join(tmpdir, "Part_10.ngc")
            file_2 = os.path.join(tmpdir, "Part_2.ngc")
            file_1 = os.path.join(tmpdir, "Part_1.ngc")

            for path in (file_10, file_2, file_1):
                with open(path, "w") as f:
                    f.write(OTHER_JOB_GCODE)

            # 1. Natural sorting: Part_1, Part_2, Part_10
            jobs = group_files_into_logical_jobs([file_10, file_2, file_1])
            names = [j.name for j in jobs]
            self.assertEqual(names, ["Part_1", "Part_2", "Part_10"])

            # 2. Caching: re-parsing should return identical results rapidly
            jobs_cached = group_files_into_logical_jobs([file_10, file_2, file_1])
            self.assertEqual([j.name for j in jobs_cached], ["Part_1", "Part_2", "Part_10"])


if __name__ == "__main__":
    unittest.main()
