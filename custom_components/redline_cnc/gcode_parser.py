"""G-code modal IO and state parser for Redline CNC (RTS-X)."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import re

_PAREN_COMMENT_RE = re.compile(r"\([^)]*\)")
_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?\d*(?:\.\d+)?)")


@dataclass(frozen=True)
class GcodeModalState:
    """Modal machine/IO state at a given G-code line."""

    spindle_on: bool = False
    spindle_cw: bool = True
    spindle_rpm: float = 0.0
    feed_rate: float = 0.0
    active_tool: int | None = None
    air_assist_on: bool = False  # M7 (Mist coolant / Air assist)
    vacuum_on: bool = False  # M8 (Flood coolant / Vacuum / Dust collection)
    aux_outputs: dict[int, bool] = field(default_factory=dict)  # M62-M65 P<n>


def _is_tool_change_shutdown(lines: list[str], next_line_idx: int) -> bool:
    """Check if an M5/M9 shutdown is followed by an M6 tool change before cutting resumes."""
    for raw in lines[next_line_idx : min(len(lines), next_line_idx + 35)]:
        clean = raw.split(";", 1)[0]
        clean = _PAREN_COMMENT_RE.sub("", clean).strip()
        if not clean or clean.startswith("%"):
            continue
        words = _WORD_RE.findall(clean)
        for letter, val in words:
            let = letter.upper()
            try:
                num = int(float(val))
            except ValueError:
                continue
            if let == "M" and num == 6:
                return True
            if let == "M" and (num in (2, 30) or num in (3, 4)):
                return False
            if let == "G" and (num in (1, 2, 3, 33, 38) or 80 <= num <= 89):
                return False
    return False


class GcodeProgramTimeline:
    """Precomputed timeline of modal G-code IO changes indexed by 1-based line number."""

    def __init__(self, text: str) -> None:
        self._line_numbers: list[int] = [0]
        self._states: list[GcodeModalState] = [GcodeModalState()]
        self.total_lines: int = 0
        self._parse(text)

    def _parse(self, text: str) -> None:
        lines = text.splitlines()
        self.total_lines = len(lines)

        spindle_on = False
        spindle_cw = True
        spindle_rpm = 0.0
        feed_rate = 0.0
        active_tool: int | None = None
        air_assist_on = False
        vacuum_on = False
        saved_vacuum_on = False
        saved_air_assist_on = False
        in_tool_change = False
        aux_outputs: dict[int, bool] = {}

        last_state = self._states[0]

        for idx, raw_line in enumerate(lines, start=1):
            # Strip semicolon comments and parenthetical comments
            line = raw_line.split(";", 1)[0]
            line = _PAREN_COMMENT_RE.sub("", line).strip()
            if not line or line.startswith("%"):
                continue

            words = [
                (letter.upper(), val)
                for letter, val in _WORD_RE.findall(line)
                if val != ""
            ]
            if not words:
                continue

            # Extract P word if present on this line (used by M62..M65)
            p_values: list[int] = []
            for letter, val in words:
                if letter == "P":
                    try:
                        p_values.append(int(float(val)))
                    except ValueError:
                        pass

            m_codes_on_line = {
                int(float(val))
                for letter, val in words
                if letter == "M" and val != ""
            }

            for letter, val in words:
                try:
                    num = float(val)
                except ValueError:
                    continue

                if letter == "S":
                    spindle_rpm = num
                elif letter == "F":
                    feed_rate = num
                elif letter == "T":
                    active_tool = int(num)
                elif letter == "G" and int(num) in (1, 2, 3):
                    if in_tool_change:
                        in_tool_change = False
                        if saved_vacuum_on:
                            vacuum_on = True
                        if saved_air_assist_on:
                            air_assist_on = True
                        saved_vacuum_on = False
                        saved_air_assist_on = False
                elif letter == "M":
                    m_code = int(num)
                    if m_code in (3, 4):
                        spindle_on = True
                        spindle_cw = (m_code == 3)
                        if in_tool_change:
                            in_tool_change = False
                            if saved_vacuum_on:
                                vacuum_on = True
                            if saved_air_assist_on:
                                air_assist_on = True
                            saved_vacuum_on = False
                            saved_air_assist_on = False
                    elif m_code == 5:
                        spindle_on = False
                        is_tc = (6 in m_codes_on_line) or _is_tool_change_shutdown(lines, idx)
                        if is_tc:
                            if vacuum_on:
                                saved_vacuum_on = True
                            if air_assist_on:
                                saved_air_assist_on = True
                            vacuum_on = False
                            air_assist_on = False
                            in_tool_change = True
                    elif m_code == 6:
                        if vacuum_on:
                            saved_vacuum_on = True
                        if air_assist_on:
                            saved_air_assist_on = True
                        spindle_on = False
                        vacuum_on = False
                        air_assist_on = False
                        in_tool_change = True
                    elif m_code == 7:
                        air_assist_on = True
                        saved_air_assist_on = True
                    elif m_code == 8:
                        vacuum_on = True
                        saved_vacuum_on = True
                    elif m_code == 9:
                        is_tc = (6 in m_codes_on_line) or _is_tool_change_shutdown(lines, idx)
                        if is_tc:
                            if vacuum_on or saved_vacuum_on:
                                saved_vacuum_on = True
                            if air_assist_on or saved_air_assist_on:
                                saved_air_assist_on = True
                            in_tool_change = True
                        else:
                            saved_vacuum_on = False
                            saved_air_assist_on = False
                            in_tool_change = False
                        air_assist_on = False
                        vacuum_on = False
                    elif m_code in (62, 64):
                        target_pins = p_values or [0]
                        aux_outputs = dict(aux_outputs)
                        for pin in target_pins:
                            aux_outputs[pin] = True
                    elif m_code in (63, 65):
                        target_pins = p_values or [0]
                        aux_outputs = dict(aux_outputs)
                        for pin in target_pins:
                            aux_outputs[pin] = False
                    elif m_code in (2, 30):
                        spindle_on = False
                        air_assist_on = False
                        vacuum_on = False
                        saved_vacuum_on = False
                        saved_air_assist_on = False
                        in_tool_change = False
                        if aux_outputs:
                            aux_outputs = {k: False for k in aux_outputs}

            new_state = GcodeModalState(
                spindle_on=spindle_on,
                spindle_cw=spindle_cw,
                spindle_rpm=spindle_rpm,
                feed_rate=feed_rate,
                active_tool=active_tool,
                air_assist_on=air_assist_on,
                vacuum_on=vacuum_on,
                aux_outputs=aux_outputs,
            )
            if new_state != last_state:
                self._line_numbers.append(idx)
                self._states.append(new_state)
                last_state = new_state

    def state_at_line(self, line_number: int) -> GcodeModalState:
        """Return the modal G-code state at 1-based `line_number`."""
        if line_number <= 0:
            return self._states[0]
        pos = bisect_right(self._line_numbers, line_number) - 1
        return self._states[max(0, pos)]
