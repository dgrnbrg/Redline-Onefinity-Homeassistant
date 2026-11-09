"""Multi-fixture batch assembler supporting Part-by-Part and Tool-by-Tool strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import re
from typing import Any

from .coolant import CoolantOverrideConfig, apply_coolant_overrides
from .job_grouper import LogicalJob
from .modal_tracker import ModalStateTracker
from .parser import GcodeLine, ToolSection, parse_gcode_words
from .pause_manager import PausePoint, inject_pauses
from .z_detector import shift_line_z, shift_lines_z

_WCS_RE = re.compile(r"\bG5[4-9](?:\.[1-3])?\b", re.IGNORECASE)


@dataclass
class FixtureSlotConfig:
    slot_id: str
    job: LogicalJob
    wcs: str = "G54"  # G54, G55, G56, etc.
    z_shift: float = 0.0
    is_air_carve: bool = False
    air_carve_height_mm: float = 25.0
    vacuum_mode: str = "default"  # "default", "force_on", "force_off"
    air_assist_mode: str = "default"  # "default", "force_on", "force_off"
    pauses: list[PausePoint] = field(default_factory=list)


@dataclass
class AssemblyOutput:
    workflow_key: str
    primary_filename: str
    files: dict[str, str]  # filename -> full gcode content
    total_tools: list[int]
    total_parts: int
    strategy: str
    has_air_carve: bool


def _clean_job_lines(lines: list[GcodeLine], target_wcs: str) -> list[GcodeLine]:
    """Strip terminators (M30/M2), % markers, and replace any hardcoded G54..G59 with target_wcs."""
    cleaned: list[GcodeLine] = []

    for line in lines:
        if line.is_terminator or line.clean_text.startswith("%"):
            continue

        # Check for existing WCS command words (G54..G59)
        has_wcs = False
        new_words = []
        for w in line.words:
            if w.letter == "G" and (54 <= int(w.value) <= 59 or 59.1 <= w.value <= 59.3):
                has_wcs = True
                target_g_val = float(target_wcs.replace("G", ""))
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


def _generate_workflow_key(slots: list[FixtureSlotConfig]) -> str:
    """Generate a clean, deterministic workflow base key (e.g. WF_bracket_clamp_G54_G55)."""
    part_names = "_".join(re.sub(r"[^a-zA-Z0-9]+", "", s.job.name.lower())[:12] for s in slots)
    wcs_list = "_".join(s.wcs for s in slots)
    return f"WF_{part_names}_{wcs_list}"


def assemble_workflow(
    slots: list[FixtureSlotConfig],
    strategy: str = "part_by_part",  # "part_by_part", "tool_by_tool", "split_by_tool"
    safe_z_command: str = "G53 G0 Z0",
    custom_epilogue: str = "",
    spindle_dwell: float = 3.0,
    timestamp_str: str | None = None,
) -> AssemblyOutput:
    """Assemble a multi-fixture workflow with safe transitions, pause injections, and epilogue."""
    if not slots:
        return AssemblyOutput(
            workflow_key="WF_empty",
            primary_filename="WF_empty.ngc",
            files={},
            total_tools=[],
            total_parts=0,
            strategy=strategy,
            has_air_carve=False,
        )

    ts = timestamp_str or datetime.datetime.now().strftime("%Y%m%d_%H%M")
    base_key = _generate_workflow_key(slots)

    all_tools: set[int] = set()
    any_air_carve = any(s.is_air_carve for s in slots)
    total_pauses = sum(len(s.pauses) for s in slots)

    detail_tags = []
    if strategy == "tool_by_tool":
        detail_tags.append("bytool")
    elif strategy == "split_by_tool":
        detail_tags.append("split")
    else:
        detail_tags.append("seq")

    if any_air_carve:
        detail_tags.append("aircarve")
    if total_pauses > 0:
        detail_tags.append(f"{total_pauses}pause")

    tag_str = "_" + "_".join(detail_tags) if detail_tags else ""

    # Common global header
    header_block = [
        f"(======================================================)",
        f"( REDLINE WORKFLOW RUN: {base_key} )",
        f"( Strategy: {strategy} | Created: {ts} )",
        f"( Slots: {len(slots)} parts across fixtures )",
        f"( Air Carve: {'ACTIVE' if any_air_carve else 'Off'} )",
        f"(======================================================)",
        "G90 G17 G94        ; Absolute mode, XY plane, Feed per minute",
        "G21                ; Units in millimeters",
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
        all_tools.update(s.job.tools_used)

    # Strategy 1: Part-by-Part (Sequential)
    if strategy == "part_by_part":
        body_lines: list[str] = []

        for idx, slot in enumerate(slots, start=1):
            effective_z_shift = slot.z_shift + (slot.air_carve_height_mm if slot.is_air_carve else 0.0)
            cleaned = _clean_job_lines(slot.job.combined_parsed_job.lines, target_wcs=slot.wcs)

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

            body_lines.append(f"\n(--- FIXTURE SLOT {idx}: {slot.job.name} -> {slot.wcs} ---)")
            body_lines.append(f"{safe_z_command} ; Safe Z retract before fixture entry")
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
            strategy=strategy,
            has_air_carve=any_air_carve,
        )

    # Strategy 2: Tool-by-Tool Grouped (Single File)
    elif strategy == "tool_by_tool":
        body_lines: list[str] = []

        for tool_num in sorted(all_tools):
            body_lines.append(f"\n(======================================================)")
            body_lines.append(f"( ACTIVE TOOL: T{tool_num} ACROSS ALL FIXTURES )")
            body_lines.append(f"(======================================================)")
            body_lines.append(f"{safe_z_command} ; Retract for tool change")
            body_lines.append(f"T{tool_num} M6           ; Tool Change to T{tool_num}")
            body_lines.append("G43 H" + str(tool_num) + "        ; Tool length offset")

            for idx, slot in enumerate(slots, start=1):
                # Find sections for this tool in this slot
                matching_sections = [
                    sec for sec in slot.job.combined_parsed_job.tool_sections if sec.tool_number == tool_num
                ]
                if not matching_sections:
                    continue

                effective_z_shift = slot.z_shift + (slot.air_carve_height_mm if slot.is_air_carve else 0.0)

                for sec in matching_sections:
                    cleaned = _clean_job_lines(sec.lines, target_wcs=slot.wcs)
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

                    body_lines.append(f"\n(--- T{tool_num} on Slot {idx} ({slot.job.name}) -> {slot.wcs} ---)")
                    body_lines.append(f"{safe_z_command} ; Safe clearance")
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
            strategy=strategy,
            has_air_carve=any_air_carve,
        )

    # Strategy 3: Split by Tool (Multiple Files)
    elif strategy == "split_by_tool":
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
                matching_sections = [
                    sec for sec in slot.job.combined_parsed_job.tool_sections if sec.tool_number == tool_num
                ]
                if not matching_sections:
                    continue

                effective_z_shift = slot.z_shift + (slot.air_carve_height_mm if slot.is_air_carve else 0.0)

                for sec in matching_sections:
                    cleaned = _clean_job_lines(sec.lines, target_wcs=slot.wcs)
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

                    tool_body.append(f"\n(--- Slot {idx} ({slot.job.name}) -> {slot.wcs} ---)")
                    tool_body.append(f"{safe_z_command}")
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
