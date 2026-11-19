"""Unit tests for multi-fixture batch assembler."""

from __future__ import annotations

import os
import tempfile
import unittest

from redline_job_linker.engine.assembler import (
    FixtureSlotConfig,
    assemble_workflow,
    replace_gcode_epilogue,
)
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

    def test_single_part_no_wcs_assembly(self):
        slots = [
            FixtureSlotConfig(slot_id="s1", job=self.job_a, wcs="none"),
        ]

        # Verify backward compatibility with single_part_no_wcs strategy string
        output = assemble_workflow(
            slots=slots,
            strategy="single_part_no_wcs",
            timestamp_str="20261108_1200",
        )

        content = output.files[output.primary_filename]
        self.assertNotIn("G54           ; Switch to fixture coordinate system", content)
        self.assertIn("Original WCS", content)

        # Verify single_part=True parameter with standard part_by_part strategy
        output2 = assemble_workflow(
            slots=slots,
            strategy="part_by_part",
            single_part=True,
            timestamp_str="20261108_1200",
        )
        content2 = output2.files[output2.primary_filename]
        self.assertNotIn("G54           ; Switch to fixture coordinate system", content2)
        self.assertIn("Original WCS", content2)
        self.assertIn("Single Part: ACTIVE", content2)

    def test_cleared_strategy_custom_sequence(self):
        # Strategy set to 'none' or None executes slots sequentially in user sequence order
        slots = [
            FixtureSlotConfig(slot_id="s1", job=self.job_a, wcs="G54"),
            FixtureSlotConfig(slot_id="s2", job=self.job_b, wcs="G55"),
        ]
        output = assemble_workflow(
            slots=slots,
            strategy="none",
            timestamp_str="20261108_1200",
        )
        self.assertEqual(output.total_parts, 2)
        content = output.files[output.primary_filename]
        self.assertIn("G54           ; Switch to fixture coordinate system", content)
        self.assertIn("G55           ; Switch to fixture coordinate system", content)
        # Part 1 appears before Part 2 in sequential order
        idx_s1 = content.find("FIXTURE SLOT 1")
        idx_s2 = content.find("FIXTURE SLOT 2")
        self.assertGreater(idx_s2, idx_s1)

    def test_custom_gcode_override_assembly(self):
        edited_code = """(Edited Bracket)
T1 M6
G0 X99.0 Y99.0 Z5.0
G1 Z-15.0 F1000
M30
"""
        slots = [
            FixtureSlotConfig(
                slot_id="s1",
                job=self.job_a,
                wcs="G54",
                custom_gcode=edited_code,
            ),
        ]

        output = assemble_workflow(
            slots=slots,
            strategy="part_by_part",
            timestamp_str="20261108_1200",
        )

        content = output.files[output.primary_filename]
        self.assertIn("X99", content)
        self.assertIn("Z-15", content)

    def test_replace_gcode_epilogue(self):
        custom_ep = "(--- CUSTOM EPILOGUE ---)\nM5\nM9\nG53 G0 Z0\nG53 G0 X0 Y800\nM30"

        # Pattern 1: G0Z1.19690 / M5 / M9 / G0Z3.00000 / G0X0.00000Y0.00000 / M2 / %
        cam_1 = "\r\n".join([
            "G20",
            "G90",
            "M3 S18000",
            "G0 X1.00000 Y1.00000",
            "G1 Z-0.12500 F50.0",
            "G1 X2.00000 Y2.00000",
            "G0Z1.19690",
            "M5",
            "M9",
            "G0Z3.00000",
            "G0X0.00000Y0.00000",
            "M2",
            "%",
            "",
        ])
        out_1 = replace_gcode_epilogue(cam_1, custom_epilogue=custom_ep)
        self.assertIn("\r\n", out_1)
        self.assertIn("G0Z1.19690", out_1)
        self.assertNotIn("G0Z3.00000", out_1)
        self.assertNotIn("G0X0.00000Y0.00000", out_1)
        self.assertNotIn("M2", out_1)
        self.assertNotIn("%", out_1)
        self.assertTrue(out_1.strip().endswith("G53 G0 X0 Y800\r\nM30"))

        # Pattern 2: G0Z1.44690 / G0Z3.25000 / G0X0.00000Y0.00000 / M5 / M9 / M2 / %
        cam_2 = "\n".join([
            "G20",
            "G90",
            "M3 S18000",
            "G1 X0.62500 Y4.33345 F100.0",
            "G0Z1.44690",
            "G0Z3.25000",
            "G0X0.00000Y0.00000",
            "",
            "M5",
            "M9",
            "M2",
            "%",
        ])
        out_2 = replace_gcode_epilogue(cam_2, custom_epilogue=custom_ep)
        self.assertNotIn("\r\n", out_2)
        self.assertIn("G0Z1.44690", out_2)
        self.assertNotIn("G0Z3.25000", out_2)
        self.assertNotIn("G0X0.00000Y0.00000", out_2)
        self.assertNotIn("M2", out_2)
        self.assertNotIn("%", out_2)
        self.assertTrue(out_2.strip().endswith("G53 G0 X0 Y800\nM30"))

    def test_assemble_strips_cam_trailing_park_move(self):
        cam_file = os.path.join(self.tmp.name, "CamPart.ngc")
        cam_gcode = "\n".join([
            "(CAM Part)",
            "T1 M6",
            "G0 X1.00000 Y1.00000 Z1.50000",
            "M3 S18000",
            "G1 Z-0.25000 F50.0",
            "G1 X10.00000 Y5.00000 F180.0",
            "G0Z1.19690",
            "M5",
            "M9",
            "G0Z3.00000",
            "G0 X0.00000 Y0.00000",
            "M2",
            "%",
        ])
        with open(cam_file, "w") as f:
            f.write(cam_gcode)

        jobs = group_files_into_logical_jobs([cam_file])
        cam_job = jobs[0]
        output = assemble_workflow(
            slots=[FixtureSlotConfig(slot_id="s1", job=cam_job, wcs="G54")],
            strategy="part_by_part",
            custom_epilogue="M5\nM9\nG53 G0 Z0\nG53 G0 X0 Y800\nM30",
            timestamp_str="20261118_1000",
        )
        content = output.files[output.primary_filename]
        self.assertIn("G0Z1.19690", content)
        self.assertNotIn("G0 X0.00000 Y0.00000", content)
        self.assertNotIn("G0Z3.00000", content)
        self.assertTrue(content.strip().endswith("G53 G0 X0 Y800\nM30"))

    def test_imperial_job_injects_g21_before_mm_park_epilogue(self):
        inch_cam = "\n".join([
            "G17 G20 G90",
            "M3 S18000",
            "G1 Z-0.25 F50",
            "G1 X10.0 Y5.0",
            "G0 Z1.5",
            "M5",
            "M2",
        ])
        # Legacy epilogue without G21 but with Y800 (mm) -> must inject G21 before G53
        legacy_ep = "M5\nM9\nG53 G0 Z0\nG53 G0 X0 Y800\nM30"
        out = replace_gcode_epilogue(inch_cam, custom_epilogue=legacy_ep)
        idx_g21 = out.find("G21")
        idx_g53 = out.find("G53 G0 Z0")
        self.assertNotEqual(idx_g21, -1)
        self.assertLess(idx_g21, idx_g53)

        # Inch-scale epilogue (Y30 < 50) -> must NOT inject G21
        inch_ep = "M5\nG53 G0 Z0\nG53 G0 X0 Y30\nM30"
        out_inch = replace_gcode_epilogue(inch_cam, custom_epilogue=inch_ep)
        self.assertNotIn("G21", out_inch)

        # Assembled imperial workflow with legacy mm epilogue -> must inject G21 before G53
        inch_file = os.path.join(self.tmp.name, "InchPart.ngc")
        with open(inch_file, "w") as f:
            f.write(inch_cam)
        inch_job = group_files_into_logical_jobs([inch_file])[0]
        assembled = assemble_workflow(
            slots=[FixtureSlotConfig(slot_id="s1", job=inch_job, wcs="G54")],
            strategy="part_by_part",
            custom_epilogue=legacy_ep,
            timestamp_str="20261118_1955",
        )
        asm_text = assembled.files[assembled.primary_filename]
        self.assertIn("G20                ; Units in inches", asm_text)
        idx_asm_g21 = asm_text.rfind("G21")
        idx_asm_g53 = asm_text.rfind("G53 G0 X0 Y800")
        self.assertNotEqual(idx_asm_g21, -1)
        self.assertLess(idx_asm_g21, idx_asm_g53)


if __name__ == "__main__":
    unittest.main()
