"""Multi-fixture batch assembler supporting Part-by-Part and Tool-by-Tool strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import re
from typing import Any

from .coolant import CoolantOverrideConfig, apply_coolant_overrides
from .job_grouper import LogicalJob
from .modal_tracker import ModalStateTracker
from .parser import GcodeLine, ParsedJob, ToolSection, parse_gcode, parse_gcode_words
from .pause_manager import PausePoint, inject_pauses
from .z_detector import shift_line_z, shift_lines_z

_WCS_RE = re.compile(r"\bG5[4-9](?:\.[1-3])?\b", re.IGNORECASE)


@dataclass
class FixtureSlotConfig:
    slot_id: str
    job: LogicalJob
    wcs: str = "G54"  # G54, G55, G56, etc., or "none" / "" to not specify/preserve
    z_shift: float = 0.0
    is_air_carve: bool = False
    air_carve_height_mm: float = 25.0
    vacuum_mode: str = "default"  # "default", "force_on", "force_off"
    air_assist_mode: str = "default"  # "default", "force_on", "force_off"
    pauses: list[PausePoint] = field(default_factory=list)
    custom_gcode: str | None = None
    op_index: int | None = None
    op_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class AssemblyOutput:
    workflow_key: str
    primary_filename: str
    files: dict[str, str]  # filename -> full gcode content
    total_tools: list[int]
    total_parts: int
    strategy: str
    has_air_carve: bool


def _clean_job_lines(lines: list[GcodeLine], target_wcs: str | None) -> list[GcodeLine]:
    """Strip terminators (M30/M2), % markers, and replace any hardcoded G54..G59 with target_wcs if target_wcs specified."""
    cleaned: list[GcodeLine] = []
    replace_wcs = bool(target_wcs and target_wcs.upper() not in ("NONE", "PRESERVE", ""))

    for line in lines:
        if line.is_terminator or line.clean_text.startswith("%"):
            continue

        if not replace_wcs:
            cleaned.append(line)
            continue

        # Check for existing WCS command words (G54..G59)
        has_wcs = False
        new_words = []
        for w in line.words:
            if w.letter == "G" and (54 <= int(w.value) <= 59 or 59.1 <= w.value <= 59.3):
                has_wcs = True
                new_words.append(f"{target_wcs}")
            else:
                new_words.append(w.raw_str)

        if has_wcs:
            reconstructed = " ".join(new_words)
            text = f"{reconstructed} ({line.comment})" if line.comment else reconstructed
            cleaned.append(
                GcodeLine(
                    line_number=line.line_number,
                    raw_text=text,
                    clean_text=reconstructed,
                    words=line.words,
                    comment=line.comment,
                    is_terminator=False,
                    is_tool_change=line.is_tool_change,
                    tool_number=line.tool_number,
                )
            )
        else:
            cleaned.append(line)

    return cleaned


def _generate_workflow_key(slots: list[FixtureSlotConfig], force_no_wcs: bool = False) -> str:
    """Generate a clean, deterministic workflow base key (e.g. WF_bracket_clamp_G54_G55)."""
    part_names = "_".join(re.sub(r"[^a-zA-Z0-9]+", "", s.job.name.lower())[:12] for s in slots)
    if force_no_wcs:
        wcs_list = "nowcs"
    else:
        wcs_list = "_".join(
            (s.wcs if s.wcs and s.wcs.upper() not in ("NONE", "") else "nowcs") for s in slots
        )
    return f"WF_{part_names}_{wcs_list}"


def _get_slot_parsed_job(slot: FixtureSlotConfig) -> ParsedJob:
    """Return slot's parsed job for its specific operation or custom G-code."""
    if slot.custom_gcode:
        return parse_gcode(slot.custom_gcode, filename=f"{slot.job.name}_edited.ngc")

    if slot.op_index is not None:
        if slot.job.is_multi_file and 1 <= slot.op_index <= len(slot.job.subfiles):
            return slot.job.subfiles[slot.op_index - 1].parsed

        start_line = slot.start_line
        end_line = slot.end_line
        if start_line is None or end_line is None:
            for op in slot.job.combined_parsed_job.operations:
                if op.op_number == slot.op_index:
                    start_line = op.start_line
                    end_line = op.end_line
                    break

        if start_line is not None and end_line is not None:
            op_lines = [
                ln.raw_text
                for ln in slot.job.combined_parsed_job.lines
                if start_line <= ln.line_number <= end_line
            ]
            if op_lines:
                return parse_gcode(
                    "\n".join(op_lines),
                    filename=f"{slot.job.name}_op{slot.op_index}.ngc",
                )

    return slot.job.combined_parsed_job


def assemble_workflow(
    slots: list[FixtureSlotConfig],
    strategy: str = "part_by_part",  # "part_by_part", "tool_by_tool", "split_by_tool"
    single_part: bool = False,
    safe_z_command: str = "G53 G0 Z0",
    custom_epilogue: str = "",
    spindle_dwell: float = 3.0,
    timestamp_str: str | None = None,
) -> AssemblyOutput:
    """Assemble a multi-fixture workflow with safe transitions, pause injections, and epilogue."""
    single_part_mode = single_part or (strategy == "single_part_no_wcs")
    if not strategy or strategy in ("single_part_no_wcs", "none", "", None, "sequential", "sequence", "as_ordered"):
        effective_strategy = "part_by_part"
    else:
        effective_strategy = strategy

    if not slots:
        return AssemblyOutput(
            workflow_key="WF_empty",
            primary_filename="WF_empty.ngc",
            files={},
            total_tools=[],
            total_parts=0,
            strategy=effective_strategy,
            has_air_carve=False,
        )

    ts = timestamp_str or datetime.datetime.now().strftime("%Y%m%d_%H%M")
    base_key = _generate_workflow_key(slots, force_no_wcs=single_part_mode)

    all_tools: set[int] = set()
    any_air_carve = any(s.is_air_carve for s in slots)
    total_pauses = sum(len(s.pauses) for s in slots)

    is_imperial = any(_get_slot_parsed_job(s).units == "imperial" for s in slots)
    unit_gcode = "G20                ; Units in inches" if is_imperial else "G21                ; Units in millimeters"

    detail_tags = []
    if effective_strategy == "tool_by_tool":
        detail_tags.append("bytool")
    elif effective_strategy == "split_by_tool":
        detail_tags.append("split")
    else:
        detail_tags.append("seq")

    if single_part_mode:
        detail_tags.append("single")

    if is_imperial:
        detail_tags.append("inch")

    if any_air_carve:
        detail_tags.append("aircarve")
    if total_pauses > 0:
        detail_tags.append(f"{total_pauses}pause")

    tag_str = "_" + "_".join(detail_tags) if detail_tags else ""

    # Common global header
    header_block = [
        f"(======================================================)",
        f"( REDLINE WORKFLOW RUN: {base_key} )",
        f"( Strategy: {effective_strategy}{' [Single Part Mode]' if single_part_mode else ''} | Created: {ts} )",
        f"( Slots: {len(slots)} parts across fixtures )",
        f"( Single Part: {'ACTIVE (Original WCS Preserved)' if single_part_mode else 'Off'} )",
        f"( Units: {'Imperial (Inches)' if is_imperial else 'Metric (Millimeters)'} )",
        f"( Air Carve: {'ACTIVE' if any_air_carve else 'Off'} )",
        f"(======================================================)",
        "G90 G17 G94        ; Absolute mode, XY plane, Feed per minute",
        unit_gcode,
        "G40 G49            ; Cutter compensation off, Tool length offset cancel",
        f"{safe_z_command}        ; Initial safe retract to machine ceiling",
        "",
    ]

    epilogue_block = [
        "",
        custom_epilogue.strip() or "(--- END OF PROGRAM ---)\nM5\nM9\nG53 G0 Z0\nM30",
        "",
    ]

    # Collect tool list
    for s in slots:
        all_tools.update(_get_slot_parsed_job(s).tools_used)

    # Strategy 1: Part-by-Part (Sequential)
    if effective_strategy == "part_by_part":
        body_lines: list[str] = []

        for idx, slot in enumerate(slots, start=1):
            slot_parsed = _get_slot_parsed_job(slot)
            air_carve_shift = (slot.air_carve_height_mm / 25.4) if is_imperial else slot.air_carve_height_mm
            effective_z_shift = slot.z_shift + (air_carve_shift if slot.is_air_carve else 0.0)

            use_wcs = (not single_part_mode) and bool(
                slot.wcs and slot.wcs.upper() not in ("NONE", "")
            )
            target_wcs = slot.wcs if use_wcs else None
            cleaned = _clean_job_lines(slot_parsed.lines, target_wcs=target_wcs)

            # Apply Z-shift
            shifted_lines = [
                GcodeLine(
                    line_number=ln.line_number,
                    raw_text=shift_line_z(ln, effective_z_shift),
                    clean_text=ln.clean_text,
                    words=ln.words,
                    comment=ln.comment,
                    is_terminator=ln.is_terminator,
                    is_tool_change=ln.is_tool_change,
                    tool_number=ln.tool_number,
                )
                for ln in cleaned
            ]

            # Modal tracking for pauses
            tracker = ModalStateTracker(shifted_lines)

            # Inject pauses
            with_pauses = inject_pauses(
                shifted_lines,
                slot.pauses,
                tracker,
                spindle_dwell=spindle_dwell,
                is_air_carve=slot.is_air_carve,
            )

            # Coolant / vacuum overrides
            coolant_cfg = CoolantOverrideConfig(
                vacuum_mode=slot.vacuum_mode,
                air_assist_mode=slot.air_assist_mode,
                is_air_carve=slot.is_air_carve,
            )
            processed_lines = apply_coolant_overrides(
                [
                    GcodeLine(i, text, text, words=parse_gcode_words(text))
                    for i, text in enumerate(with_pauses, 1)
                ],
                coolant_cfg,
            )

            wcs_label = slot.wcs if use_wcs else "Original WCS"
            body_lines.append(f"\n(--- FIXTURE SLOT {idx}: {slot.job.name} -> {wcs_label} ---)")
            body_lines.append(f"{safe_z_command} ; Safe Z retract before fixture entry")
            if use_wcs:
                body_lines.append(f"{slot.wcs}           ; Switch to fixture coordinate system")
            body_lines.extend(processed_lines)
            body_lines.append(f"{safe_z_command} ; Safe Z retract after fixture complete")

        full_content = "\n".join(header_block + body_lines + epilogue_block)
        filename = f"{base_key}{tag_str}.ngc"
        return AssemblyOutput(
            workflow_key=base_key,
            primary_filename=filename,
            files={filename: full_content},
            total_tools=sorted(all_tools),
            total_parts=len(slots),
            strategy=effective_strategy,
            has_air_carve=any_air_carve,
        )

    # Strategy 2: Tool-by-Tool Grouped (Single File)
    elif effective_strategy == "tool_by_tool":
        body_lines: list[str] = []

        for tool_num in sorted(all_tools):
            body_lines.append(f"\n(======================================================)")
            body_lines.append(f"( ACTIVE TOOL: T{tool_num} ACROSS ALL FIXTURES )")
            body_lines.append(f"(======================================================)")
            body_lines.append(f"{safe_z_command} ; Retract for tool change")
            body_lines.append(f"T{tool_num} M6           ; Tool Change to T{tool_num}")
            body_lines.append("G43 H" + str(tool_num) + "        ; Tool length offset")

            for idx, slot in enumerate(slots, start=1):
                slot_parsed = _get_slot_parsed_job(slot)
                # Find sections for this tool in this slot
                matching_sections = [
                    sec for sec in slot_parsed.tool_sections if sec.tool_number == tool_num
                ]
                if not matching_sections:
                    continue

                air_carve_shift = (slot.air_carve_height_mm / 25.4) if is_imperial else slot.air_carve_height_mm
                effective_z_shift = slot.z_shift + (air_carve_shift if slot.is_air_carve else 0.0)

                use_wcs = (not single_part_mode) and bool(
                    slot.wcs and slot.wcs.upper() not in ("NONE", "")
                )
                target_wcs = slot.wcs if use_wcs else None

                for sec in matching_sections:
                    cleaned = _clean_job_lines(sec.lines, target_wcs=target_wcs)
                    shifted = [
                        GcodeLine(
                            line_number=ln.line_number,
                            raw_text=shift_line_z(ln, effective_z_shift),
                            clean_text=ln.clean_text,
                            words=ln.words,
                            comment=ln.comment,
                            is_terminator=ln.is_terminator,
                            is_tool_change=ln.is_tool_change,
                            tool_number=ln.tool_number,
                        )
                        for ln in cleaned
                    ]

                    # Map pauses for this section
                    sec_pauses = [p for p in slot.pauses if sec.start_line <= p.line_number <= sec.end_line]
                    tracker = ModalStateTracker(shifted)
                    with_pauses = inject_pauses(
                        shifted,
                        sec_pauses,
                        tracker,
                        spindle_dwell=spindle_dwell,
                        is_air_carve=slot.is_air_carve,
                    )

                    coolant_cfg = CoolantOverrideConfig(
                        vacuum_mode=slot.vacuum_mode,
                        air_assist_mode=slot.air_assist_mode,
                        is_air_carve=slot.is_air_carve,
                    )
                    processed = apply_coolant_overrides(
                        [GcodeLine(i, t, t, words=parse_gcode_words(t)) for i, t in enumerate(with_pauses, 1)],
                        coolant_cfg,
                    )

                    wcs_desc = slot.wcs if use_wcs else "Original WCS"
                    body_lines.append(f"\n(--- T{tool_num} on Slot {idx} ({slot.job.name}) -> {wcs_desc} ---)")
                    body_lines.append(f"{safe_z_command} ; Safe clearance")
                    if use_wcs:
                        body_lines.append(f"{slot.wcs}           ; Switch to fixture")
                    body_lines.extend(processed)
                    body_lines.append(f"{safe_z_command} ; Clear workpiece")

        full_content = "\n".join(header_block + body_lines + epilogue_block)
        filename = f"{base_key}{tag_str}.ngc"
        return AssemblyOutput(
            workflow_key=base_key,
            primary_filename=filename,
            files={filename: full_content},
            total_tools=sorted(all_tools),
            total_parts=len(slots),
            strategy=effective_strategy,
            has_air_carve=any_air_carve,
        )

    # Strategy 3: Split by Tool (Multiple Files)
    elif effective_strategy == "split_by_tool":
        files_dict: dict[str, str] = {}
        first_fname = ""

        for tool_num in sorted(all_tools):
            tool_body: list[str] = [
                f"\n(======================================================)",
                f"( TOOL {tool_num} PASS ACROSS FIXTURES )",
                f"(======================================================)",
                f"{safe_z_command}",
                f"T{tool_num} M6",
                f"G43 H{tool_num}",
            ]

            for idx, slot in enumerate(slots, start=1):
                slot_parsed = _get_slot_parsed_job(slot)
                matching_sections = [
                    sec for sec in slot_parsed.tool_sections if sec.tool_number == tool_num
                ]
                if not matching_sections:
                    continue

                air_carve_shift = (slot.air_carve_height_mm / 25.4) if is_imperial else slot.air_carve_height_mm
                effective_z_shift = slot.z_shift + (air_carve_shift if slot.is_air_carve else 0.0)

                use_wcs = (not single_part_mode) and bool(
                    slot.wcs and slot.wcs.upper() not in ("NONE", "")
                )
                target_wcs = slot.wcs if use_wcs else None

                for sec in matching_sections:
                    cleaned = _clean_job_lines(sec.lines, target_wcs=target_wcs)
                    shifted = [
                        GcodeLine(
                            line_number=ln.line_number,
                            raw_text=shift_line_z(ln, effective_z_shift),
                            clean_text=ln.clean_text,
                            words=ln.words,
                            comment=ln.comment,
                            is_terminator=ln.is_terminator,
                            is_tool_change=ln.is_tool_change,
                            tool_number=ln.tool_number,
                        )
                        for ln in cleaned
                    ]
                    sec_pauses = [p for p in slot.pauses if sec.start_line <= p.line_number <= sec.end_line]
                    tracker = ModalStateTracker(shifted)
                    with_pauses = inject_pauses(
                        shifted,
                        sec_pauses,
                        tracker,
                        spindle_dwell=spindle_dwell,
                        is_air_carve=slot.is_air_carve,
                    )
                    coolant_cfg = CoolantOverrideConfig(
                        vacuum_mode=slot.vacuum_mode,
                        air_assist_mode=slot.air_assist_mode,
                        is_air_carve=slot.is_air_carve,
                    )
                    processed = apply_coolant_overrides(
                        [GcodeLine(i, t, t, words=parse_gcode_words(t)) for i, t in enumerate(with_pauses, 1)],
                        coolant_cfg,
                    )

                    wcs_desc = slot.wcs if use_wcs else "Original WCS"
                    tool_body.append(f"\n(--- Slot {idx} ({slot.job.name}) -> {wcs_desc} ---)")
                    tool_body.append(f"{safe_z_command}")
                    if use_wcs:
                        tool_body.append(f"{slot.wcs}")
                    tool_body.extend(processed)
                    tool_body.append(f"{safe_z_command}")

            fname = f"{base_key}{tag_str}_T{tool_num}.ngc"
            if not first_fname:
                first_fname = fname
            files_dict[fname] = "\n".join(header_block + tool_body + epilogue_block)

        return AssemblyOutput(
            workflow_key=base_key,
            primary_filename=first_fname,
            files=files_dict,
            total_tools=sorted(all_tools),
            total_parts=len(slots),
            strategy=strategy,
            has_air_carve=any_air_carve,
        )

    raise ValueError(f"Unknown strategy: {strategy}")
