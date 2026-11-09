"""Unit tests for coolant and vacuum overrides and Air Carve suppression."""

from __future__ import annotations

import unittest

from redline_job_linker.engine.coolant import CoolantOverrideConfig, apply_coolant_overrides
from redline_job_linker.engine.parser import GcodeLine, parse_gcode_words

SAMPLE_COOLANT_LINES = [
    GcodeLine(1, "M3 S18000", "M3 S18000", parse_gcode_words("M3 S18000")),
    GcodeLine(2, "M8 (Flood Vacuum On)", "M8", parse_gcode_words("M8"), comment="Flood Vacuum On"),
    GcodeLine(3, "M7 (Mist Coolant On)", "M7", parse_gcode_words("M7"), comment="Mist Coolant On"),
    GcodeLine(4, "G1 X10.0 Y10.0 F1200", "G1 X10.0 Y10.0 F1200", parse_gcode_words("G1 X10.0 Y10.0 F1200")),
    GcodeLine(5, "M9", "M9", parse_gcode_words("M9")),
]


class TestCoolantOverrides(unittest.TestCase):
    def test_air_carve_suppression(self):
        # When Air Carve is active, all M7 and M8 must be suppressed
        cfg = CoolantOverrideConfig(is_air_carve=True)
        res = apply_coolant_overrides(SAMPLE_COOLANT_LINES, cfg)
        full_text = "\n".join(res)
        self.assertNotIn("M8\n", full_text)
        self.assertNotIn("M7\n", full_text)
        self.assertIn("Air Carve: suppressed", full_text)

    def test_force_off_vacuum(self):
        cfg = CoolantOverrideConfig(vacuum_mode="force_off", air_assist_mode="default")
        res = apply_coolant_overrides(SAMPLE_COOLANT_LINES, cfg)
        full_text = "\n".join(res)
        self.assertIn("M7", full_text)
        self.assertIn("Suppressed M8", full_text)


if __name__ == "__main__":
    unittest.main()
