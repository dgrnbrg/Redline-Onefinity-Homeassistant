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


if __name__ == "__main__":
    unittest.main()
