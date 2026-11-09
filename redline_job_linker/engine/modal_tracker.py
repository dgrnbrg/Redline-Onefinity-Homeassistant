"""Accurate line-by-line G-code modal state tracking."""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import GcodeLine


@dataclass(frozen=True)
class ModalStateSnapshot:
    line_number: int
    spindle_on: bool = False
    spindle_cw: bool = True
    spindle_rpm: float = 0.0
    feed_rate: float = 0.0
    active_tool: int = 1
    vacuum_on: bool = False
    air_assist_on: bool = False
    wcs: str = "G54"
    distance_mode: str = "G90"  # G90 (absolute) or G91 (incremental)
    current_x: float = 0.0
    current_y: float = 0.0
    current_z: float = 0.0


class ModalStateTracker:
    """Pre-computes and caches modal state across all lines in a job."""

    def __init__(self, lines: list[GcodeLine]) -> None:
        self._states: list[ModalStateSnapshot] = []
        self._build(lines)

    def _build(self, lines: list[GcodeLine]) -> None:
        spindle_on = False
        spindle_cw = True
        spindle_rpm = 0.0
        feed_rate = 0.0
        active_tool = 1
        vacuum_on = False
        air_assist_on = False
        wcs = "G54"
        dist_mode = "G90"
        cur_x = 0.0
        cur_y = 0.0
        cur_z = 0.0

        for line in lines:
            has_g53 = any(w.letter == "G" and int(w.value) == 53 for w in line.words)

            for w in line.words:
                let = w.letter
                val = w.value

                if let == "M":
                    m_val = int(val)
                    if m_val == 3:
                        spindle_on = True
                        spindle_cw = True
                    elif m_val == 4:
                        spindle_on = True
                        spindle_cw = False
                    elif m_val == 5:
                        spindle_on = False
                    elif m_val == 7:
                        air_assist_on = True
                    elif m_val == 8:
                        vacuum_on = True
                    elif m_val == 9:
                        air_assist_on = False
                        vacuum_on = False
                    elif m_val in (2, 30):
                        spindle_on = False
                        air_assist_on = False
                        vacuum_on = False
                elif let == "S":
                    spindle_rpm = val
                elif let == "F":
                    feed_rate = val
                elif let == "T":
                    active_tool = int(val)
                elif let == "G":
                    g_val = int(val)
                    if g_val == 90:
                        dist_mode = "G90"
                    elif g_val == 91:
                        dist_mode = "G91"
                    elif 54 <= g_val <= 59:
                        wcs = f"G{g_val}"
                elif not has_g53:
                    if let == "X":
                        cur_x = val
                    elif let == "Y":
                        cur_y = val
                    elif let == "Z":
                        cur_z = val

            self._states.append(
                ModalStateSnapshot(
                    line_number=line.line_number,
                    spindle_on=spindle_on,
                    spindle_cw=spindle_cw,
                    spindle_rpm=spindle_rpm,
                    feed_rate=feed_rate,
                    active_tool=active_tool,
                    vacuum_on=vacuum_on,
                    air_assist_on=air_assist_on,
                    wcs=wcs,
                    distance_mode=dist_mode,
                    current_x=cur_x,
                    current_y=cur_y,
                    current_z=cur_z,
                )
            )

    def get_state_at(self, line_number: int) -> ModalStateSnapshot:
        """Get modal state snapshot at given 1-based line number."""
        if not self._states:
            return ModalStateSnapshot(line_number=line_number)
        idx = max(0, min(len(self._states) - 1, line_number - 1))
        return self._states[idx]
