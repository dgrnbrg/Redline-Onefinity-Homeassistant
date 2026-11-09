"""G-code parsing, AST normalization, and toolpath geometry extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

_PAREN_COMMENT_RE = re.compile(r"\((.*?)\)")
_SEMICOLON_COMMENT_RE = re.compile(r";(.*)$")
_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?\d*(?:\.\d+)?)")

# Common CAM terminator patterns to strip from intermediate jobs
_TERMINATOR_M_CODES = {2, 30}


@dataclass
class GcodeWord:
    letter: str
    value: float
    raw_str: str


@dataclass
class GcodeLine:
    line_number: int
    raw_text: str
    clean_text: str
    words: list[GcodeWord] = field(default_factory=list)
    comment: str = ""
    is_terminator: bool = False
    is_tool_change: bool = False
    tool_number: int | None = None


@dataclass
class ToolSection:
    tool_number: int
    description: str
    start_line: int
    end_line: int
    lines: list[GcodeLine] = field(default_factory=list)
    min_z: float = 0.0
    max_z: float = 0.0
    vacuum_active: bool = False
    air_assist_active: bool = False


@dataclass
class ToolpathPoint:
    x: float
    y: float
    z: float
    motion_type: str  # 'rapid' (G0), 'linear' (G1), 'arc' (G2/G3)
    line_number: int
    tool: int
    feed: float


@dataclass
class OperationInfo:
    op_number: int
    name: str
    tool_number: int
    start_line: int
    end_line: int
    min_z: float
    max_z: float
    min_cutting_z: float
    max_cutting_z: float
    total_lines: int


@dataclass
class ParsedJob:
    filename: str
    total_lines: int
    lines: list[GcodeLine]
    tool_sections: list[ToolSection]
    tools_used: list[int]
    tool_descriptions: dict[int, str]
    header_comments: list[str]
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float
    min_cutting_z: float
    max_cutting_z: float
    total_cutting_moves: int
    negative_cutting_moves: int
    positive_cutting_moves: int
    toolpath_geometry: list[ToolpathPoint]
    units: str = "metric"  # "metric" (G21) or "imperial" (G20)
    safe_z_threshold: float = 0.0
    operations: list[OperationInfo] = field(default_factory=list)


def parse_gcode_words(clean_line: str) -> list[GcodeWord]:
    """Parse words (letter + number) from a clean G-code line."""
    words: list[GcodeWord] = []
    for letter, val in _WORD_RE.findall(clean_line):
        if not letter or val == "":
            continue
        try:
            num = float(val)
        except ValueError:
            continue
        words.append(GcodeWord(letter=letter.upper(), value=num, raw_str=f"{letter.upper()}{val}"))
    return words


def parse_gcode(content: str, filename: str = "job.ngc") -> ParsedJob:
    """Parse G-code text, normalize AST, extract tools and 3D toolpath geometry."""
    raw_lines = content.splitlines()
    parsed_lines: list[GcodeLine] = []
    header_comments: list[str] = []

    # State tracking
    units = "metric"
    active_tool = 1
    tool_descriptions: dict[int, str] = {}
    tools_used: set[int] = set()

    current_x = 0.0
    current_y = 0.0
    current_z = 0.0
    active_motion = "rapid"
    active_feed = 0.0

    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")
    min_z = float("inf")
    max_z = float("-inf")

    min_cutting_z = float("inf")
    max_cutting_z = float("-inf")
    total_cutting_moves = 0
    negative_cutting_moves = 0
    positive_cutting_moves = 0

    geometry: list[ToolpathPoint] = []
    current_section_lines: list[GcodeLine] = []
    sections: list[ToolSection] = []

    # 1. Parse lines and scan comments / headers
    for idx, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.strip()
        comment_parts: list[str] = []

        # Extract paren comments
        for p in _PAREN_COMMENT_RE.findall(line):
            comment_parts.append(p.strip())
        line_no_paren = _PAREN_COMMENT_RE.sub(" ", line)

        # Extract semicolon comments
        semi_match = _SEMICOLON_COMMENT_RE.search(line_no_paren)
        if semi_match:
            comment_parts.append(semi_match.group(1).strip())
            clean = line_no_paren[: semi_match.start()].strip()
        else:
            clean = line_no_paren.strip()

        combined_comment = " | ".join(comment_parts)
        if idx <= 50 and combined_comment:
            header_comments.append(combined_comment)
            if re.search(r"\b(?:inch(?:es)?|imperial)\b", combined_comment, re.IGNORECASE):
                units = "imperial"
            elif re.search(r"\b(?:mm|millimeters?|metric)\b", combined_comment, re.IGNORECASE):
                units = "metric"

        # Extract tool descriptions from comments like (Tool 1: 1/4" Endmill) or T1 - 1/4 Flat
        tool_desc_match = re.search(r"(?:Tool|T)\s*(\d+)\s*[:\-]\s*([^|)]+)", combined_comment, re.IGNORECASE)
        if tool_desc_match:
            try:
                t_num = int(tool_desc_match.group(1))
                tool_descriptions[t_num] = tool_desc_match.group(2).strip()
            except ValueError:
                pass

        # Check for % wrapper
        if clean.startswith("%"):
            parsed_lines.append(
                GcodeLine(
                    line_number=idx,
                    raw_text=raw_line,
                    clean_text="",
                    words=[],
                    comment=combined_comment,
                    is_terminator=True,
                )
            )
            continue

        words = parse_gcode_words(clean)

        is_term = False
        is_tc = False
        t_word: int | None = None

        has_g53 = False
        has_motion = False
        new_x = current_x
        new_y = current_y
        new_z = current_z

        for w in words:
            if w.letter == "G":
                g_val = int(w.value)
                if g_val == 20:
                    units = "imperial"
                elif g_val == 21:
                    units = "metric"
                elif g_val == 53:
                    has_g53 = True
                elif g_val == 0:
                    active_motion = "rapid"
                    has_motion = True
                elif g_val == 1:
                    active_motion = "linear"
                    has_motion = True
                elif g_val in (2, 3):
                    active_motion = "arc"
                    has_motion = True
            elif w.letter == "M":
                m_val = int(w.value)
                if m_val in _TERMINATOR_M_CODES:
                    is_term = True
                elif m_val == 6:
                    is_tc = True
            elif w.letter == "T":
                t_word = int(w.value)
                tools_used.add(t_word)
                active_tool = t_word
            elif w.letter == "F":
                active_feed = w.value
            elif w.letter == "X":
                new_x = w.value
                has_motion = True
            elif w.letter == "Y":
                new_y = w.value
                has_motion = True
            elif w.letter == "Z":
                new_z = w.value
                has_motion = True

        g_line = GcodeLine(
            line_number=idx,
            raw_text=raw_line,
            clean_text=clean,
            words=words,
            comment=combined_comment,
            is_terminator=is_term,
            is_tool_change=is_tc,
            tool_number=t_word or active_tool,
        )
        parsed_lines.append(g_line)

        # Update geometry and statistics if motion occurred (ignoring machine coordinate G53 moves for bounding box)
        if has_motion and not has_g53:
            current_x = new_x
            current_y = new_y
            current_z = new_z

            min_x = min(min_x, current_x)
            max_x = max(max_x, current_x)
            min_y = min(min_y, current_y)
            max_y = max(max_y, current_y)
            min_z = min(min_z, current_z)
            max_z = max(max_z, current_z)

            is_cutting = active_motion in ("linear", "arc")
            if is_cutting:
                total_cutting_moves += 1
                min_cutting_z = min(min_cutting_z, current_z)
                max_cutting_z = max(max_cutting_z, current_z)
                if current_z <= 0.0001:
                    negative_cutting_moves += 1
                else:
                    positive_cutting_moves += 1

            geometry.append(
                ToolpathPoint(
                    x=round(current_x, 4),
                    y=round(current_y, 4),
                    z=round(current_z, 4),
                    motion_type=active_motion,
                    line_number=idx,
                    tool=active_tool,
                    feed=active_feed,
                )
            )

    # 2. Divide into tool sections
    first_tool = 1
    for line in parsed_lines:
        if line.tool_number is not None:
            first_tool = line.tool_number
            break

    cur_tool = first_tool
    cur_sec_lines: list[GcodeLine] = []
    sec_start = 1

    for line in parsed_lines:
        if line.is_tool_change and line.tool_number is not None and line.tool_number != cur_tool:
            if cur_sec_lines:
                sections.append(
                    ToolSection(
                        tool_number=cur_tool,
                        description=tool_descriptions.get(cur_tool, f"Tool {cur_tool}"),
                        start_line=sec_start,
                        end_line=line.line_number - 1,
                        lines=cur_sec_lines,
                    )
                )
                cur_sec_lines = []
            cur_tool = line.tool_number
            sec_start = line.line_number

        cur_sec_lines.append(line)

    if cur_sec_lines and cur_tool is not None:
        sections.append(
            ToolSection(
                tool_number=cur_tool,
                description=tool_descriptions.get(cur_tool, f"Tool {cur_tool}"),
                start_line=sec_start,
                end_line=len(parsed_lines),
                lines=cur_sec_lines,
            )
        )

    if not tools_used:
        tools_used.add(1)

    operations, safe_z_threshold = _infer_operations(
        parsed_lines=parsed_lines,
        geometry=geometry,
        units=units,
        tool_descriptions=tool_descriptions,
    )

    return ParsedJob(
        filename=filename,
        total_lines=len(raw_lines),
        lines=parsed_lines,
        tool_sections=sections,
        tools_used=sorted(tools_used),
        tool_descriptions=tool_descriptions,
        header_comments=header_comments,
        min_x=0.0 if min_x == float("inf") else round(min_x, 4),
        max_x=0.0 if max_x == float("-inf") else round(max_x, 4),
        min_y=0.0 if min_y == float("inf") else round(min_y, 4),
        max_y=0.0 if max_y == float("-inf") else round(max_y, 4),
        min_z=0.0 if min_z == float("inf") else round(min_z, 4),
        max_z=0.0 if max_z == float("-inf") else round(max_z, 4),
        min_cutting_z=0.0 if min_cutting_z == float("inf") else round(min_cutting_z, 4),
        max_cutting_z=0.0 if max_cutting_z == float("-inf") else round(max_cutting_z, 4),
        total_cutting_moves=total_cutting_moves,
        negative_cutting_moves=negative_cutting_moves,
        positive_cutting_moves=positive_cutting_moves,
        toolpath_geometry=geometry,
        units=units,
        safe_z_threshold=safe_z_threshold,
        operations=operations,
    )


def _infer_safe_z_threshold(
    geometry: list[ToolpathPoint],
    units: str,
) -> float:
    """Infer the safe Z retract height boundary separating local stepover rapids from clearance retracts."""
    all_rapid_z = [round(pt.z, 4) for pt in geometry if pt.motion_type == "rapid"]
    if not all_rapid_z:
        return 0.0

    cutting_points = [pt for pt in geometry if pt.motion_type in ("linear", "arc")]
    max_cut_z = max((pt.z for pt in cutting_points), default=0.0)
    distinct_rapid_z = sorted(list(set(all_rapid_z)))

    if len(distinct_rapid_z) == 1:
        return distinct_rapid_z[0]

    # Reference surface: top of cutting or stock zero (whichever is higher)
    ref_surface = max(0.0, max_cut_z)
    stepover_margin = 2.0 if units == "metric" else 0.08

    stepovers = [z for z in distinct_rapid_z if z <= ref_surface + stepover_margin]
    clearances = [z for z in distinct_rapid_z if z > ref_surface + stepover_margin]

    if stepovers and clearances:
        # Halfway between highest stepover rapid and lowest clearance retract
        return round((max(stepovers) + min(clearances)) / 2.0, 4)
    elif clearances:
        # If all rapids are above ref_surface + margin, stepover is lowest level
        if len(distinct_rapid_z) > 1:
            return round((distinct_rapid_z[0] + distinct_rapid_z[1]) / 2.0, 4)
        return distinct_rapid_z[0]
    else:
        # All rapids are low; threshold between lowest and highest rapid
        return round((distinct_rapid_z[0] + distinct_rapid_z[-1]) / 2.0, 4)


def _infer_operations(
    parsed_lines: list[GcodeLine],
    geometry: list[ToolpathPoint],
    units: str,
    tool_descriptions: dict[int, str],
    safe_z_threshold: float | None = None,
) -> tuple[list[OperationInfo], float]:
    """Infer distinct machining operations by tool changes and safe Z retracts."""
    if not parsed_lines:
        return [], 0.0

    if safe_z_threshold is None:
        safe_z_threshold = _infer_safe_z_threshold(geometry, units)

    cutting_points = [pt for pt in geometry if pt.motion_type in ("linear", "arc")]
    cutting_lines = set(pt.line_number for pt in cutting_points)
    max_cutting_line = max(cutting_lines, default=0)

    rapid_high_lines = set(
        pt.line_number for pt in geometry if pt.motion_type == "rapid" and pt.z >= safe_z_threshold
    )

    first_tool = 1
    for line in parsed_lines:
        if line.tool_number is not None:
            first_tool = line.tool_number
            break

    split_points: list[tuple[int, int]] = []  # (line_number, new_tool_number)
    current_tool = first_tool
    has_cut_in_op = False

    for line in parsed_lines:
        # 1. Tool change delineates an op
        if line.is_tool_change and line.tool_number is not None and line.tool_number != current_tool:
            split_points.append((line.line_number, line.tool_number))
            current_tool = line.tool_number
            has_cut_in_op = False
            continue

        # 2. Check for cutting move
        if line.line_number in cutting_lines or any(w.letter == "G" and int(w.value) in (1, 2, 3) for w in line.words):
            has_cut_in_op = True

        # 3. Retract to safe Z height (higher Z retract than a shallow single-op stepover)
        if has_cut_in_op and line.line_number < max_cutting_line:
            is_safe_retract = line.line_number in rapid_high_lines or (
                any(w.letter == "G" and int(w.value) == 0 for w in line.words)
                and any(w.letter == "Z" and w.value >= safe_z_threshold for w in line.words)
            )
            if is_safe_retract:
                split_points.append((line.line_number, current_tool))
                has_cut_in_op = False

    # Deduplicate adjacent split points
    deduped_splits: list[tuple[int, int]] = []
    for sp in split_points:
        if not deduped_splits or sp[0] > deduped_splits[-1][0] + 1:
            deduped_splits.append(sp)

    # Build ranges: [1, s1-1], [s1, s2-1], ..., [sN, total_lines]
    ranges: list[tuple[int, int, int]] = []
    prev_start = 1
    prev_tool = first_tool

    for sp_line, sp_tool in deduped_splits:
        ranges.append((prev_start, sp_line - 1, prev_tool))
        prev_start = sp_line
        prev_tool = sp_tool

    ranges.append((prev_start, len(parsed_lines), prev_tool))

    operations: list[OperationInfo] = []
    line_map = {line.line_number: line for line in parsed_lines}

    for op_idx, (s_line, e_line, tool_num) in enumerate(ranges, start=1):
        op_geom = [pt for pt in geometry if s_line <= pt.line_number <= e_line]
        op_cuts = [pt for pt in op_geom if pt.motion_type in ("linear", "arc")]

        min_z = min((pt.z for pt in op_geom), default=0.0)
        max_z = max((pt.z for pt in op_geom), default=0.0)
        min_cut = min((pt.z for pt in op_cuts), default=0.0)
        max_cut = max((pt.z for pt in op_cuts), default=0.0)

        # Look for comment near operation start
        op_name = ""
        for l_num in range(s_line, min(len(parsed_lines) + 1, s_line + 8)):
            gl = line_map.get(l_num)
            if gl and gl.comment:
                c = gl.comment.strip()
                if not re.match(r"^(?:G\d+|M\d+|Units|Time|Date|Setup|Post|Created)\b", c, re.IGNORECASE):
                    cleaned = re.sub(r"^[|\-:\s]+", "", c).strip()
                    if cleaned:
                        op_name = cleaned
                        break

        if not op_name:
            tool_desc = tool_descriptions.get(tool_num, f"T{tool_num}")
            op_name = f"Op {op_idx}: {tool_desc}"

        operations.append(
            OperationInfo(
                op_number=op_idx,
                name=op_name,
                tool_number=tool_num,
                start_line=s_line,
                end_line=e_line,
                min_z=round(min_z, 4),
                max_z=round(max_z, 4),
                min_cutting_z=round(min_cut, 4),
                max_cutting_z=round(max_cut, 4),
                total_lines=e_line - s_line + 1,
            )
        )

    # Filter out empty intermediate operations (no cutting moves when file has cutting moves)
    total_cutting_in_file = len(cutting_points)
    if total_cutting_in_file > 0:
        filtered_ops: list[OperationInfo] = []
        for op in operations:
            op_cuts = [
                pt
                for pt in geometry
                if op.start_line <= pt.line_number <= op.end_line
                and pt.motion_type in ("linear", "arc")
            ]
            if not op_cuts and filtered_ops:
                # Merge empty transition into previous op
                prev = filtered_ops[-1]
                filtered_ops[-1] = OperationInfo(
                    op_number=prev.op_number,
                    name=prev.name,
                    tool_number=prev.tool_number,
                    start_line=prev.start_line,
                    end_line=op.end_line,
                    min_z=min(prev.min_z, op.min_z),
                    max_z=max(prev.max_z, op.max_z),
                    min_cutting_z=prev.min_cutting_z,
                    max_cutting_z=prev.max_cutting_z,
                    total_lines=op.end_line - prev.start_line + 1,
                )
            else:
                filtered_ops.append(op)

        # Renumber operations
        for new_idx, op in enumerate(filtered_ops, start=1):
            op.op_number = new_idx
        return filtered_ops, safe_z_threshold

    return operations, safe_z_threshold
