"""Unit tests for Z-origin detection and Z-shifting."""

from __future__ import annotations

import unittest

from redline_job_linker.engine.parser import GcodeLine, parse_gcode, parse_gcode_words
from redline_job_linker.engine.z_detector import detect_z_origin, shift_line_z, shift_lines_z

VECTRIC_TOP_GCODE = """(Vectric VCarve)
(| Z Zero: Top of Material |)
G0 Z5.0
G1 Z-6.35 F600
G1 X10.0 Y10.0
M30
"""

VECTRIC_BED_GCODE = """(Vectric VCarve)
(| Z Zero: Machine Bed |)
G0 Z25.0
G1 Z12.0 F600
G1 Z6.0
G1 Z0.1
G1 Z-0.5
M30
"""

VECTRIC_BED_EXCEEDED_GCODE = """(Vectric VCarve)
(| Z Zero: Machine Bed |)
G0 Z25.0
G1 Z12.0 F600
G1 Z-5.0
M30
"""

GENERIC_TOP_GCODE = """G90 G21
G0 Z5.0
G1 Z-1.0 F500
G1 Z-2.0
G1 Z-3.0
G1 Z-4.0
G0 Z5.0
M30
"""

GENERIC_BED_GCODE = """G90 G21
G0 Z30.0
G1 Z20.0 F500
G1 Z15.0
G1 Z10.0
G1 Z5.0
G1 Z0.0
G0 Z30.0
M30
"""


class TestZDetector(unittest.TestCase):
    def test_vectric_top_detection(self):
        job = parse_gcode(VECTRIC_TOP_GCODE)
        analysis = detect_z_origin(job, spoilboard_max_cut_depth_mm=1.27)
        self.assertEqual(analysis.origin, "top_of_stock")
        self.assertGreaterEqual(analysis.confidence, 0.9)
        self.assertFalse(analysis.spoilboard_exceeded)

    def test_vectric_bed_detection(self):
        job = parse_gcode(VECTRIC_BED_GCODE)
        analysis = detect_z_origin(job, spoilboard_max_cut_depth_mm=1.27)
        self.assertEqual(analysis.origin, "machine_bed")
        self.assertGreaterEqual(analysis.confidence, 0.9)
        self.assertFalse(analysis.spoilboard_exceeded)

    def test_spoilboard_limit_violation(self):
        # Cuts down to -5.0mm, exceeding 1.27mm (0.05 in) limit
        job = parse_gcode(VECTRIC_BED_EXCEEDED_GCODE)
        analysis = detect_z_origin(job, spoilboard_max_cut_depth_mm=1.27)
        self.assertEqual(analysis.origin, "unknown")
        self.assertTrue(analysis.spoilboard_exceeded)
        self.assertIn("exceeding max spoilboard cut-through", analysis.rationale)

    def test_generic_depth_heuristics(self):
        job_top = parse_gcode(GENERIC_TOP_GCODE)
        analysis_top = detect_z_origin(job_top, spoilboard_max_cut_depth_mm=1.27)
        self.assertEqual(analysis_top.origin, "top_of_stock")

        job_bed = parse_gcode(GENERIC_BED_GCODE)
        analysis_bed = detect_z_origin(job_bed, spoilboard_max_cut_depth_mm=1.27)
        self.assertEqual(analysis_bed.origin, "machine_bed")

    def test_safe_z_shifting(self):
        # Normal cutting move: should shift
        normal_line = GcodeLine(
            line_number=1,
            raw_text="G1 Z-5.000 F600",
            clean_text="G1 Z-5.000 F600",
            words=parse_gcode_words("G1 Z-5.000 F600"),
        )
        shifted = shift_line_z(normal_line, delta_z=25.0)
        self.assertIn("Z20", shifted)

        # Machine coordinate move: MUST NEVER shift!
        g53_line = GcodeLine(
            line_number=2,
            raw_text="G53 G0 Z0",
            clean_text="G53 G0 Z0",
            words=parse_gcode_words("G53 G0 Z0"),
        )
        shifted_g53 = shift_line_z(g53_line, delta_z=25.0)
        self.assertEqual(shifted_g53, "G53 G0 Z0")


if __name__ == "__main__":
    unittest.main()
