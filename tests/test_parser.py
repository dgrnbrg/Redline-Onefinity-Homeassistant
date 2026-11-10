"""Unit tests for G-code parser and geometry extraction."""

from __future__ import annotations

import unittest

from redline_job_linker.engine.parser import parse_gcode, parse_gcode_words

SAMPLE_GCODE_1 = """(Vectric VCarve Pro)
(Tool 1: 1/4" Downcut Endmill)
G90 G17 G21
G0 Z5.000
G0 X10.000 Y20.000
M3 S18000
M8
G1 Z-3.175 F600
G1 X50.000 Y20.000 F1800
G1 X50.000 Y50.000
G0 Z5.000
M5
M9
G53 G0 Z0
M30
"""

SAMPLE_MULTI_TOOL_GCODE = """(Fusion 360 CAM)
G90 G21
T1 M6 (Tool 1: 1/4 Flat)
G0 Z10.0
M3 S16000
G1 Z-2.0 F500
G1 X20.0 Y0 F1500
G0 Z10.0
M5
T2 M6 (Tool 2: 1/8 Ball)
G0 Z10.0
M3 S20000
G1 Z-1.0 F400
G1 X20.0 Y10.0 F1200
G0 Z10.0
M5
M30
"""


class TestGcodeParser(unittest.TestCase):
    def test_parse_gcode_words(self):
        words = parse_gcode_words("G1 X10.5 Y-20.25 Z0 F1200")
        self.assertEqual(len(words), 5)
        self.assertEqual(words[0].letter, "G")
        self.assertEqual(words[0].value, 1.0)
        self.assertEqual(words[1].letter, "X")
        self.assertEqual(words[1].value, 10.5)
        self.assertEqual(words[2].letter, "Y")
        self.assertEqual(words[2].value, -20.25)
        self.assertEqual(words[3].letter, "Z")
        self.assertEqual(words[3].value, 0.0)
        self.assertEqual(words[4].letter, "F")
        self.assertEqual(words[4].value, 1200.0)

    def test_single_tool_job_parsing(self):
        job = parse_gcode(SAMPLE_GCODE_1, filename="test_part.ngc")
        self.assertEqual(job.filename, "test_part.ngc")
        self.assertEqual(job.units, "metric")
        self.assertEqual(job.tools_used, [1])
        self.assertIn(1, job.tool_descriptions)
        self.assertIn("1/4", job.tool_descriptions[1])
        self.assertAlmostEqual(job.min_cutting_z, -3.175, places=3)
        self.assertAlmostEqual(job.max_x, 50.0, places=3)
        self.assertEqual(job.total_cutting_moves, 3)
        self.assertEqual(job.negative_cutting_moves, 3)
        self.assertEqual(job.positive_cutting_moves, 0)
        self.assertTrue(len(job.toolpath_geometry) >= 4)

    def test_multi_tool_sections(self):
        job = parse_gcode(SAMPLE_MULTI_TOOL_GCODE, filename="multi_tool.ngc")
        self.assertEqual(job.tools_used, [1, 2])
        self.assertEqual(len(job.tool_sections), 2)
        self.assertEqual(job.tool_sections[0].tool_number, 1)
        self.assertEqual(job.tool_sections[1].tool_number, 2)

    def test_imperial_units_detection(self):
        imperial_code = """(Vectric Aspire)
( Units: Inches )
G90 G20
G0 Z1.0
G1 Z-0.25 F30
M30
"""
        job = parse_gcode(imperial_code)
        self.assertEqual(job.units, "imperial")
        self.assertAlmostEqual(job.min_cutting_z, -0.25)

    def test_inferred_operations_delineation(self):
        gcode = """
( Roughing Pocket )
G20
G0 Z0.8
G0 X0 Y0
G1 Z-0.2 F30
G1 X10.0
G0 Z0.05
G0 X10.1
G1 Z-0.2
( Safe Z Retract finishes op 1 )
G0 Z0.8
( Finishing Profile )
G0 X20.0 Y20.0
G1 Z-0.5 F40
G1 X30.0
G0 Z0.8
( Tool Change to T2 )
M6 T2
G0 X0 Y0
G1 Z-0.1 F20
G1 X5.0
G0 Z0.8
M30
"""
        job = parse_gcode(gcode)
        self.assertEqual(job.units, "imperial")
        self.assertEqual(len(job.operations), 3)
        self.assertEqual(job.operations[0].tool_number, 1)
        self.assertEqual(job.operations[1].tool_number, 1)
        self.assertEqual(job.operations[2].tool_number, 2)
        self.assertAlmostEqual(job.operations[0].min_cutting_z, -0.2)
        self.assertAlmostEqual(job.operations[1].min_cutting_z, -0.5)
        self.assertAlmostEqual(job.operations[2].min_cutting_z, -0.1)

    def test_safe_z_threshold_and_stepover_differentiation(self):
        # Program with stepovers at Z=0.03 and safe retracts at Z=0.30
        gcode = """G20
G0 X0 Y0 Z0.3
G1 Z-0.1 F20
G1 X1.0
G0 Z0.03
G0 X1.05
G1 Z-0.1
G0 Z0.3
G0 X5.0 Y5.0
G1 Z-0.2
G0 Z0.3
M30
"""
        job = parse_gcode(gcode)
        # Threshold should be between 0.03 and 0.30 (approx 0.165)
        self.assertGreater(job.safe_z_threshold, 0.03)
        self.assertLess(job.safe_z_threshold, 0.30)
        self.assertEqual(len(job.operations), 2)

    def test_conservative_safe_z_inference_with_small_hops(self):
        # Program with only small stepover hops (gap <= 1.0mm)
        # Should NOT split into multiple ops automatically
        gcode = """G21
G0 X0 Y0 Z1.0
G1 Z-2.0 F300
G1 X10.0
G0 Z1.5
G0 X12.0
G1 Z-2.0
G1 X20.0
G0 Z1.5
M30
"""
        job = parse_gcode(gcode)
        # Safe Z threshold should be placed conservatively above max rapid (1.5 + 2.0 = 3.5)
        self.assertGreaterEqual(job.safe_z_threshold, 3.5)
        self.assertEqual(len(job.operations), 1)

    def test_conservative_safe_z_inference_with_routine_clearance_hops(self):
        # Program with multiple passes that repeatedly retract to 5.0mm clearance
        # Even with a 4.0mm gap between Z1.0 hop and Z5.0 clearance, repeated retracts
        # must NOT be treated as operation splits.
        gcode = """G21
G0 X0 Y0 Z5.0
G1 Z-2.0 F300
G1 X10.0
G0 Z1.0
G0 X12.0
G1 Z-2.0
G1 X20.0
G0 Z5.0
G0 X22.0
G1 Z-2.0
G1 X30.0
G0 Z1.0
G0 X32.0
G1 Z-2.0
G1 X40.0
G0 Z5.0
G0 X42.0
G1 Z-2.0
G1 X50.0
G0 Z5.0
M30
"""
        job = parse_gcode(gcode)
        # Threshold should be conservatively placed above 5.0 (5.0 + 2.0 = 7.0)
        self.assertGreaterEqual(job.safe_z_threshold, 7.0)
        self.assertEqual(len(job.operations), 1)

    def test_conservative_safe_z_inference_with_isolated_op_retract(self):
        # Program with routine hops at Z=1.0 and Z=5.0, but an isolated high clearance retract
        # to Z=25.0 between two distinct machining sections.
        gcode = """G21
G0 X0 Y0 Z5.0
G1 Z-2.0 F300
G1 X10.0
G0 Z1.0
G0 X12.0
G1 Z-2.0
G1 X20.0
G0 Z25.0
G0 X50.0 Y50.0
G1 Z-2.0
G1 X60.0
G0 Z1.0
G0 X62.0
G1 Z-2.0
G1 X70.0
G0 Z25.0
M30
"""
        job = parse_gcode(gcode)
        # Threshold should be between 5.0 and 25.0 (approx 15.0)
        self.assertGreater(job.safe_z_threshold, 5.0)
        self.assertLess(job.safe_z_threshold, 25.0)
        self.assertEqual(len(job.operations), 2)
        self.assertEqual(job.operations[0].end_line, 8)
        self.assertEqual(job.operations[1].start_line, 9)

    def test_manual_op_splits(self):
        # Program with single op, then manually split at line 5 (the G0 Z1.5 rapid)
        gcode = """G21
G0 X0 Y0 Z1.0
G1 Z-2.0 F300
G1 X10.0
G0 Z1.5
G0 X12.0
G1 Z-2.0
G1 X20.0
G0 Z1.5
M30
"""
        job = parse_gcode(gcode)
        self.assertEqual(len(job.operations), 1)

        from redline_job_linker.engine.parser import _infer_operations
        ops, _ = _infer_operations(
            parsed_lines=job.lines,
            geometry=job.toolpath_geometry,
            units=job.units,
            tool_descriptions=job.tool_descriptions,
            safe_z_threshold=job.safe_z_threshold,
            manual_splits=[5],
        )
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0].start_line, 1)
        self.assertEqual(ops[0].end_line, 4)
        self.assertEqual(ops[1].start_line, 5)


if __name__ == "__main__":
    unittest.main()
