"""Modal-aware pause injection with safe Z and gantry park retract options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .modal_tracker import ModalStateSnapshot, ModalStateTracker
from .parser import GcodeLine


@dataclass
class PausePoint:
    line_number: int
    pause_type: str = "M0"  # "M0" (unconditional pause) or "M1" (optional stop)
    message: str = ""
    retract_mode: str = "safe_z"  # "safe_z" or "gantry_park"
    custom_park_x: float = 0.0
    custom_park_y: float = 800.0


def inject_pauses(
    lines: list[GcodeLine],
    pauses: list[PausePoint],
    tracker: ModalStateTracker,
    spindle_dwell: float = 3.0,
    is_air_carve: bool = False,
) -> list[str]:
    """Inject modal-aware pauses into lines at specified line numbers."""
    if not pauses:
        return [ln.raw_text for ln in lines]

    # Map target line numbers to pause definitions
    pause_map: dict[int, list[PausePoint]] = {}
    for p in pauses:
        pause_map.setdefault(p.line_number, []).append(p)

    output: list[str] = []

    for line in lines:
        output.append(line.raw_text)

        if line.line_number in pause_map:
            for p in pause_map[line.line_number]:
                state = tracker.get_state_at(line.line_number)

                output.append(f"\n(--- VISUAL PAUSE: {p.pause_type} ---)")

                # 1. Safe shutdown of active motion and tools
                if state.spindle_on:
                    output.append("M5               ; Spindle Stop for pause")
                if state.vacuum_on or state.air_assist_on:
                    output.append("M9               ; Coolant/Vacuum Off for pause")

                # 2. Retract
                output.append("G53 G0 Z0        ; Safe Z retract to machine ceiling")
                if p.retract_mode == "gantry_park":
                    output.append(
                        f"G53 G0 X{p.custom_park_x:g} Y{p.custom_park_y:g}   ; Move gantry out of way for pause"
                    )

                # 3. Pause statement with optional message
                msg_comment = f" (MSG, {p.message})" if p.message else ""
                output.append(f"{p.pause_type}{msg_comment} ; Pause execution (hit Cycle Start to resume)")

                output.append("(--- RESUMING AFTER PAUSE ---)")

                # 4. Return to position if gantry was parked
                if p.retract_mode == "gantry_park":
                    output.append(
                        f"{state.wcs} G0 X{state.current_x:g} Y{state.current_y:g} ; Return to pre-pause XY"
                    )

                # 5. Restart spindle if it was active
                if state.spindle_on:
                    spindle_cmd = "M3" if state.spindle_cw else "M4"
                    rpm_str = f" S{state.spindle_rpm:g}" if state.spindle_rpm > 0 else ""
                    output.append(f"{spindle_cmd}{rpm_str} ; Restart spindle at active RPM")
                    if spindle_dwell > 0:
                        output.append(f"G4 P{spindle_dwell:g}          ; Dwell for spindle spin-up")

                # 6. Restore vacuum and air assist if they were active (and not air carve)
                if not is_air_carve:
                    if state.vacuum_on:
                        output.append("M8               ; Restore Vacuum/Flood")
                    if state.air_assist_on:
                        output.append("M7               ; Restore Air Assist/Mist")

                # 7. Rapid back down to clearance height above workpiece before cut resumes
                output.append(f"G0 Z{state.current_z:g}          ; Return to pre-pause Z")
                output.append("(--- END PAUSE BLOCK ---)\n")

    return output
