"""Unit tests for multi-fixture batch assembler."""

from __future__ import annotations

import os
import tempfile
import unittest

from redline_job_linker.engine.assembler import FixtureSlotConfig, assemble_workflow
from redline_job_linker.engine.job_grouper import group_files_into_logical_jobs

PART_A_GCODE = """(Part A)
T1 M6
G0 X0 Y0 Z5.0
M3 S18000
M8
G1 Z-3.0 F500
G1 X50.0 Y0 F1500
G0 Z5.0
M5
M9
T2 M6
G0 X0 Y0 Z5.0
M3 S20000
G1 Z-1.0 F300
G1 X50.0 Y10.0
G0 Z5.0
M5
M30
"""

PART_B_GCODE = """(Part B)
T1 M6
G0 X0 Y0 Z5.0
M3 S18000
M8
G1 Z-2.0 F500
G1 X40.0 Y20.0
G0 Z5.0
M5
M9
T2 M6
G0 X0 Y0 Z5.0
M3 S20000
G1 Z-0.5 F300
G1 X40.0 Y30.0
G0 Z5.0
M5
M30
"""


class TestAssembler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file_a = os.path.join(self.tmp.name, "Bracket.ngc")
        self.file_b = os.path.join(self.tmp.name, "Plate.ngc")
        with open(self.file_a, "w") as f:
            f.write(PART_A_GCODE)
        with open(self.file_b, "w") as f:
            f.write(PART_B_GCODE)

        jobs = group_files_into_logical_jobs([self.file_a, self.file_b])
        self.job_a = next(j for j in jobs if j.name == "Bracket")
        self.job_b = next(j for j in jobs if j.name == "Plate")

    def tearDown(self):
        self.tmp.cleanup()

    def test_part_by_part_assembly(self):
        slots = [
            FixtureSlotConfig(slot_id="s1", job=self.job_a, wcs="G54"),
            FixtureSlotConfig(slot_id="s2", job=self.job_b, wcs="G55"),
        ]

        output = assemble_workflow(
            slots=slots,
            strategy="part_by_part",
            custom_epilogue="G53 G0 X0 Y800\nM30",
            timestamp_str="20261108_1200",
        )

        self.assertIn("WF_bracket_plate_G54_G55", output.workflow_key)
        self.assertIn(output.primary_filename, output.files)
        content = output.files[output.primary_filename]

        # Check WCS switching
        self.assertIn("G54           ; Switch to fixture coordinate system", content)
        self.assertIn("G55           ; Switch to fixture coordinate system", content)

        # Check safe transition retracts
        self.assertIn("G53 G0 Z0 ; Safe Z retract", content)

        # Custom epilogue at end
        self.assertTrue(content.strip().endswith("G53 G0 X0 Y800\nM30"))

        # Intermediate M30 stripped
        lines = content.splitlines()
        m30_count = sum(1 for ln in lines if ln.strip() == "M30")
        self.assertEqual(m30_count, 1)

    def test_tool_by_tool_assembly(self):
        slots = [
            FixtureSlotConfig(slot_id="s1", job=self.job_a, wcs="G54"),
            FixtureSlotConfig(slot_id="s2", job=self.job_b, wcs="G55"),
        ]

        output = assemble_workflow(
            slots=slots,
            strategy="tool_by_tool",
            timestamp_str="20261108_1200",
        )

        content = output.files[output.primary_filename]

        # Should group by tool: T1 header first, then T2 header
        idx_t1 = content.find("ACTIVE TOOL: T1 ACROSS ALL FIXTURES")
        idx_t2 = content.find("ACTIVE TOOL: T2 ACROSS ALL FIXTURES")
        self.assertNotEqual(idx_t1, -1)
        self.assertNotEqual(idx_t2, -1)
        self.assertLess(idx_t1, idx_t2)

        # T1 should run on both G54 and G55
        t1_section = content[idx_t1:idx_t2]
        self.assertIn("T1 on Slot 1 (Bracket) -> G54", t1_section)
        self.assertIn("T1 on Slot 2 (Plate) -> G55", t1_section)

    def test_air_carve_assembly(self):
        slots = [
            FixtureSlotConfig(
                slot_id="s1",
                job=self.job_a,
                wcs="G54",
                is_air_carve=True,
                air_carve_height_mm=25.0,
            ),
        ]

        output = assemble_workflow(
            slots=slots,
            strategy="part_by_part",
            timestamp_str="20261108_1200",
        )

        self.assertTrue(output.has_air_carve)
        self.assertIn("aircarve", output.primary_filename)
        content = output.files[output.primary_filename]

        # Cutting depth was -3.0mm; with +25mm air carve, cutting moves should be at Z22
        self.assertIn("Z22", content)
        # M8 should be suppressed
        self.assertNotIn("M8\n", content)


if __name__ == "__main__":
    unittest.main()
