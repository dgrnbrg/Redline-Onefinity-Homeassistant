"""Unit tests for modal-aware pause injection and retract behavior."""

from __future__ import annotations

import unittest

from redline_job_linker.engine.modal_tracker import ModalStateTracker
from redline_job_linker.engine.parser import parse_gcode
from redline_job_linker.engine.pause_manager import PausePoint, inject_pauses

SAMPLE_CUT_FOR_PAUSE = """G90 G21
G0 X0 Y0 Z5.0
M3 S18000
M8
G1 Z-2.0 F600
G1 X50.0 Y0 F1500
G1 X50.0 Y50.0
G0 Z5.0
M5
M9
M30
"""


class TestPauseManager(unittest.TestCase):
    def test_safe_z_pause_injection(self):
        job = parse_gcode(SAMPLE_CUT_FOR_PAUSE)
        tracker = ModalStateTracker(job.lines)

        # Inject pause right after G1 X50.0 Y0 (line 6)
        pause = PausePoint(
            line_number=6,
            pause_type="M0",
            message="Check clamp clearance",
            retract_mode="safe_z",
        )

        output = inject_pauses(job.lines, [pause], tracker, spindle_dwell=3.0, is_air_carve=False)
        text = "\n".join(output)

        self.assertIn("M0 (MSG, Check clamp clearance)", text)
        self.assertIn("M5               ; Spindle Stop for pause", text)
        self.assertIn("M9               ; Coolant/Vacuum Off for pause", text)
        self.assertIn("G53 G0 Z0        ; Safe Z retract to machine ceiling", text)
        self.assertIn("M3 S18000 ; Restart spindle at active RPM", text)
        self.assertIn("G4 P3          ; Dwell for spindle spin-up", text)
        self.assertIn("M8               ; Restore Vacuum/Flood", text)

    def test_gantry_park_retract_mode(self):
        job = parse_gcode(SAMPLE_CUT_FOR_PAUSE)
        tracker = ModalStateTracker(job.lines)

        pause = PausePoint(
            line_number=6,
            pause_type="M0",
            message="Insert threaded nuts",
            retract_mode="gantry_park",
            custom_park_x=0.0,
            custom_park_y=800.0,
        )

        output = inject_pauses(job.lines, [pause], tracker, spindle_dwell=2.0)
        text = "\n".join(output)

        self.assertIn("G53 G0 X0 Y800   ; Move gantry out of way for pause", text)
        self.assertIn("G0 X50 Y0 ; Return to pre-pause XY", text)


if __name__ == "__main__":
    unittest.main()
