"""Z-origin heuristic analysis, spoilboard limit checking, and safe Z-coordinate shifting."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .parser import GcodeLine, ParsedJob

# Regular expressions for popular CAM headers
_VECTRIC_BED_RE = re.compile(r"Z\s*Zero\s*:\s*Machine\s*Bed", re.IGNORECASE)
_VECTRIC_TOP_RE = re.compile(r"Z\s*Zero\s*:\s*Top\s*of\s*Material", re.IGNORECASE)
_FUSION_TOP_RE = re.compile(r"Stock\s*:\s*.*Z\s*max\s*:\s*0(?:\.0+)?\b", re.IGNORECASE)
_FUSION_BED_RE = re.compile(r"Stock\s*:\s*.*Z\s*min\s*:\s*0(?:\.0+)?\b", re.IGNORECASE)


@dataclass
class ZOriginAnalysis:
    origin: str  # "top_of_stock", "machine_bed", "unknown"
    label: str  # Human readable label
    confidence: float  # 0.0 to 1.0
    rationale: str
    min_cutting_z: float
    max_cutting_z: float
    spoilboard_limit_mm: float
    spoilboard_exceeded: bool


def detect_z_origin(
    job: ParsedJob,
    spoilboard_max_cut_depth_mm: float = 1.27,
) -> ZOriginAnalysis:
    """Guess whether the job's Z-zero was set on the top of the part or machine bed.

    Rules:
      1. Inspect CAM header comments (Vectric, Fusion 360, Carveco).
      2. If cutting passes are below Z=0 by more than spoilboard_max_cut_depth_mm,
         it cannot be a bed-origin job (classified as top_of_stock or unknown).
      3. If all cutting passes are positive (down to ~0.0 or within spoilboard limit),
         it is classified as machine_bed.
      4. If vast majority of cutting passes are negative, classified as top_of_stock.
    """
    all_comments = " ".join(job.header_comments)
    min_z = job.min_cutting_z
    max_z = job.max_cutting_z
    total_cuts = job.total_cutting_moves
    unit_label = "in" if job.units == "imperial" else "mm"
    spoilboard_limit = (
        spoilboard_max_cut_depth_mm / 25.4
        if job.units == "imperial"
        else spoilboard_max_cut_depth_mm
    )

    # Spoilboard limit check
    spoilboard_exceeded = min_z < -abs(spoilboard_limit)

    # 1. Comment inspection
    if _VECTRIC_BED_RE.search(all_comments) or _FUSION_BED_RE.search(all_comments):
        if spoilboard_exceeded:
            return ZOriginAnalysis(
                origin="unknown",
                label="Unknown / Review Required",
                confidence=0.5,
                rationale=(
                    f"CAM header says Machine Bed, but cutting plunges to Z={min_z:.3f} {unit_label}, "
                    f"exceeding max spoilboard cut-through limit ({spoilboard_limit:.3f} {unit_label})."
                ),
                min_cutting_z=min_z,
                max_cutting_z=max_z,
                spoilboard_limit_mm=spoilboard_limit,
                spoilboard_exceeded=True,
            )
        return ZOriginAnalysis(
            origin="machine_bed",
            label="Machine Bed (Stock Bottom)",
            confidence=0.98,
            rationale="Detected explicit 'Z Zero: Machine Bed' or 'Stock Z min: 0' in CAM header.",
            min_cutting_z=min_z,
            max_cutting_z=max_z,
            spoilboard_limit_mm=spoilboard_limit,
            spoilboard_exceeded=False,
        )

    if _VECTRIC_TOP_RE.search(all_comments) or _FUSION_TOP_RE.search(all_comments):
        return ZOriginAnalysis(
            origin="top_of_stock",
            label="Top of Part (Stock Top)",
            confidence=0.98,
            rationale="Detected explicit 'Z Zero: Top of Material' or 'Stock Z max: 0' in CAM header.",
            min_cutting_z=min_z,
            max_cutting_z=max_z,
            spoilboard_limit_mm=spoilboard_limit,
            spoilboard_exceeded=False,
        )

    # 2. Coordinate range heuristics
    if total_cuts == 0:
        return ZOriginAnalysis(
            origin="unknown",
            label="Unknown (No cutting moves)",
            confidence=0.2,
            rationale="No G1/G2/G3 cutting motion found in file.",
            min_cutting_z=min_z,
            max_cutting_z=max_z,
            spoilboard_limit_mm=spoilboard_limit,
            spoilboard_exceeded=False,
        )

    neg_ratio = job.negative_cutting_moves / total_cuts
    pos_ratio = job.positive_cutting_moves / total_cuts

    # Top of Stock: Cutting passes are almost entirely negative (Z <= 0)
    if neg_ratio >= 0.85:
        return ZOriginAnalysis(
            origin="top_of_stock",
            label="Top of Part (Stock Top)",
            confidence=round(0.85 + (neg_ratio * 0.1), 2),
            rationale=(
                f"{neg_ratio * 100:.1f}% of cutting moves are Z <= 0 (min Z: {min_z:.3f} {unit_label}, "
                f"max Z: {max_z:.3f} {unit_label}). Typical of part-top zeroing."
            ),
            min_cutting_z=min_z,
            max_cutting_z=max_z,
            spoilboard_limit_mm=spoilboard_limit,
            spoilboard_exceeded=False,
        )

    # Machine Bed: Cutting passes are predominantly positive stepping down towards 0
    if pos_ratio >= 0.70:
        if spoilboard_exceeded:
            return ZOriginAnalysis(
                origin="unknown",
                label="Unknown / Review Required",
                confidence=0.4,
                rationale=(
                    f"Mostly positive cutting passes ({pos_ratio * 100:.1f}%), but deepest cut "
                    f"is Z={min_z:.3f} {unit_label}, exceeding spoilboard threshold of -{spoilboard_limit:.3f} {unit_label}."
                ),
                min_cutting_z=min_z,
                max_cutting_z=max_z,
                spoilboard_limit_mm=spoilboard_limit,
                spoilboard_exceeded=True,
            )
        return ZOriginAnalysis(
            origin="machine_bed",
            label="Machine Bed (Stock Bottom)",
            confidence=round(0.80 + (pos_ratio * 0.15), 2),
            rationale=(
                f"{pos_ratio * 100:.1f}% of cutting moves are Z > 0, terminating at Z={min_z:.3f} {unit_label} "
                f"(within allowable spoilboard clearance -{spoilboard_limit:.3f} {unit_label})."
            ),
            min_cutting_z=min_z,
            max_cutting_z=max_z,
            spoilboard_limit_mm=spoilboard_limit,
            spoilboard_exceeded=False,
        )

    return ZOriginAnalysis(
        origin="unknown",
        label="Unknown / Ambiguous",
        confidence=0.3,
        rationale=(
            f"Mixed cutting depths (neg: {neg_ratio * 100:.1f}%, pos: {pos_ratio * 100:.1f}%, "
            f"min Z: {min_z:.3f} {unit_label}, max Z: {max_z:.3f} {unit_label}). Please select manually."
        ),
        min_cutting_z=min_z,
        max_cutting_z=max_z,
        spoilboard_limit_mm=spoilboard_limit,
        spoilboard_exceeded=False,
    )


def shift_line_z(line: GcodeLine, delta_z: float) -> str:
    """Shift any non-G53 Z words in a G-code line by delta_z.

    Preserves G53 machine coordinate moves (e.g. G53 G0 Z0) strictly unchanged!
    """
    if abs(delta_z) < 1e-6:
        return line.raw_text

    # Safety check: do NOT modify G53 machine coordinate lines
    has_g53 = any(w.letter == "G" and int(w.value) == 53 for w in line.words)
    if has_g53:
        return line.raw_text

    has_z = any(w.letter == "Z" for w in line.words)
    if not has_z:
        return line.raw_text

    # Reconstruct clean line replacing Z word
    tokens: list[str] = []
    for w in line.words:
        if w.letter == "Z":
            new_z = round(w.value + delta_z, 4)
            # Format float nicely without trailing zeros if integer
            tokens.append(f"Z{new_z:g}")
        else:
            tokens.append(w.raw_str)

    reconstructed = " ".join(tokens)
    if line.comment:
        return f"{reconstructed} ({line.comment})"
    return reconstructed


def shift_lines_z(lines: list[GcodeLine], delta_z: float) -> list[str]:
    """Shift all applicable lines by delta_z."""
    if abs(delta_z) < 1e-6:
        return [ln.raw_text for ln in lines]
    return [shift_line_z(ln, delta_z) for ln in lines]
