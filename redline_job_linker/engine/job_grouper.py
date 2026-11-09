"""Multi-file CAM job detector and logical job grouper."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

from .parser import ParsedJob, ToolSection, parse_gcode
from .z_detector import ZOriginAnalysis, detect_z_origin

# Regex patterns matching common CAM toolpath sequence suffixes
_TOOLPATH_SUFFIX_PATTERNS = [
    # e.g. "Bracket_1_Roughing.ngc", "Bracket_2_Finishing.ngc"
    re.compile(r"^(.*?)[_\-\s]+(?:(\d+)|(?:T(\d+)))[_\-\s]+(.*)$", re.IGNORECASE),
    # e.g. "Bracket - T1.ngc", "Bracket - T2.ngc"
    re.compile(r"^(.*?)[_\-\s]+T(\d+)$", re.IGNORECASE),
    # e.g. "Bracket_Roughing.ngc", "Bracket_Finishing.ngc"
    re.compile(r"^(.*?)[_\-\s]+(rough(?:ing)?|finish(?:ing)?|contour|pocket|drill(?:ing)?|chamfer|vcarve|detail)$", re.IGNORECASE),
]


@dataclass
class LogicalJobSubfile:
    filepath: str
    filename: str
    sequence_order: int
    operation_name: str
    tool_number: int
    parsed: ParsedJob


@dataclass
class LogicalJob:
    id: str
    name: str
    is_multi_file: bool
    subfiles: list[LogicalJobSubfile]
    tools_used: list[int]
    total_lines: int
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float
    min_cutting_z: float
    max_cutting_z: float
    z_origin: ZOriginAnalysis
    combined_parsed_job: ParsedJob

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "is_multi_file": self.is_multi_file,
            "subfiles_count": len(self.subfiles),
            "subfiles": [
                {
                    "filename": sf.filename,
                    "order": sf.sequence_order,
                    "op": sf.operation_name,
                    "tool": sf.tool_number,
                    "lines": sf.parsed.total_lines,
                }
                for sf in self.subfiles
            ],
            "tools_used": self.tools_used,
            "total_lines": self.total_lines,
            "extents": {
                "min_x": self.min_x,
                "max_x": self.max_x,
                "min_y": self.min_y,
                "max_y": self.max_y,
                "min_z": self.min_z,
                "max_z": self.max_z,
                "min_cutting_z": self.min_cutting_z,
                "max_cutting_z": self.max_cutting_z,
            },
            "z_origin": {
                "origin": self.z_origin.origin,
                "label": self.z_origin.label,
                "confidence": self.z_origin.confidence,
                "rationale": self.z_origin.rationale,
                "spoilboard_exceeded": self.z_origin.spoilboard_exceeded,
            },
        }


def _extract_base_name_and_order(filename: str) -> tuple[str, int, str]:
    """Attempt to extract common base name, sequence order, and op name."""
    stem = Path(filename).stem

    for pat in _TOOLPATH_SUFFIX_PATTERNS:
        match = pat.match(stem)
        if match:
            groups = match.groups()
            base = groups[0].strip().rstrip("-_ ")
            order = 1
            op = stem
            if len(groups) >= 2 and groups[1] and groups[1].isdigit():
                order = int(groups[1])
            elif len(groups) >= 3 and groups[2] and groups[2].isdigit():
                order = int(groups[2])
            if len(groups) >= 4 and groups[3]:
                op = groups[3].strip()
            elif len(groups) >= 2 and groups[1] and not groups[1].isdigit():
                op = groups[1].strip()
            return base, order, op

    return stem, 1, stem


def group_files_into_logical_jobs(
    file_paths: list[str],
    spoilboard_max_cut_depth_mm: float = 1.27,
) -> list[LogicalJob]:
    """Group file paths into LogicalJobs, detecting multi-file CAM exports."""
    # Read and parse each file
    parsed_subfiles: list[tuple[str, str, ParsedJob, float]] = []  # (path, name, parsed, mtime)
    for p in file_paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            mtime = os.path.getmtime(p) if os.path.exists(p) else 0.0
            name = os.path.basename(p)
            parsed = parse_gcode(content, filename=name)
            parsed_subfiles.append((p, name, parsed, mtime))
        except Exception:
            continue

    # Bucket by candidate base name
    buckets: dict[str, list[tuple[str, str, ParsedJob, int, str, float]]] = {}
    for p, name, parsed, mtime in parsed_subfiles:
        base, order, op = _extract_base_name_and_order(name)
        buckets.setdefault(base.lower(), []).append((p, name, parsed, order, op, mtime))

    logical_jobs: list[LogicalJob] = []

    for base_lower, items in buckets.items():
        # Sort items by sequence order, then by filename
        items.sort(key=lambda it: (it[3], it[1]))

        is_multi = len(items) > 1
        base_name = Path(items[0][1]).stem if not is_multi else _extract_base_name_and_order(items[0][1])[0]

        subfiles: list[LogicalJobSubfile] = []
        all_tools: set[int] = set()
        tot_lines = 0

        min_x = float("inf")
        max_x = float("-inf")
        min_y = float("inf")
        max_y = float("-inf")
        min_z = float("inf")
        max_z = float("-inf")
        min_cut_z = float("inf")
        max_cut_z = float("-inf")

        combined_lines = []
        combined_sections: list[ToolSection] = []
        all_geometry = []

        for p, name, parsed, order, op, mtime in items:
            tool_num = parsed.tools_used[0] if parsed.tools_used else 1
            subfiles.append(
                LogicalJobSubfile(
                    filepath=p,
                    filename=name,
                    sequence_order=order,
                    operation_name=op,
                    tool_number=tool_num,
                    parsed=parsed,
                )
            )
            all_tools.update(parsed.tools_used)
            tot_lines += parsed.total_lines

            min_x = min(min_x, parsed.min_x)
            max_x = max(max_x, parsed.max_x)
            min_y = min(min_y, parsed.min_y)
            max_y = max(max_y, parsed.max_y)
            min_z = min(min_z, parsed.min_z)
            max_z = max(max_z, parsed.max_z)
            min_cut_z = min(min_cut_z, parsed.min_cutting_z)
            max_cut_z = max(max_cut_z, parsed.max_cutting_z)

            combined_lines.extend(parsed.lines)
            combined_sections.extend(parsed.tool_sections)
            all_geometry.extend(parsed.toolpath_geometry)

        # Build synthesized parsed job for the unified logical job
        combined_job = ParsedJob(
            filename=f"{base_name}.ngc",
            total_lines=tot_lines,
            lines=combined_lines,
            tool_sections=combined_sections,
            tools_used=sorted(all_tools),
            tool_descriptions=items[0][2].tool_descriptions,
            header_comments=items[0][2].header_comments,
            min_x=0.0 if min_x == float("inf") else min_x,
            max_x=0.0 if max_x == float("-inf") else max_x,
            min_y=0.0 if min_y == float("inf") else min_y,
            max_y=0.0 if max_y == float("-inf") else max_y,
            min_z=0.0 if min_z == float("inf") else min_z,
            max_z=0.0 if max_z == float("-inf") else max_z,
            min_cutting_z=0.0 if min_cut_z == float("inf") else min_cut_z,
            max_cutting_z=0.0 if max_cut_z == float("-inf") else max_cut_z,
            total_cutting_moves=sum(it[2].total_cutting_moves for it in items),
            negative_cutting_moves=sum(it[2].negative_cutting_moves for it in items),
            positive_cutting_moves=sum(it[2].positive_cutting_moves for it in items),
            toolpath_geometry=all_geometry,
            units=items[0][2].units,
        )

        z_analysis = detect_z_origin(
            combined_job,
            spoilboard_max_cut_depth_mm=spoilboard_max_cut_depth_mm,
        )

        job_id = f"job_{base_name.lower().replace(' ', '_')}"

        logical_jobs.append(
            LogicalJob(
                id=job_id,
                name=base_name,
                is_multi_file=is_multi,
                subfiles=subfiles,
                tools_used=sorted(all_tools),
                total_lines=tot_lines,
                min_x=combined_job.min_x,
                max_x=combined_job.max_x,
                min_y=combined_job.min_y,
                max_y=combined_job.max_y,
                min_z=combined_job.min_z,
                max_z=combined_job.max_z,
                min_cutting_z=combined_job.min_cutting_z,
                max_cutting_z=combined_job.max_cutting_z,
                z_origin=z_analysis,
                combined_parsed_job=combined_job,
            )
        )

    return sorted(logical_jobs, key=lambda j: j.name.lower())
