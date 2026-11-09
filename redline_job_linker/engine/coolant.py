"""Coolant and vacuum modifier, supporting per-section overrides and Air Carve suppression."""

from __future__ import annotations

from dataclasses import dataclass

from .parser import GcodeLine


@dataclass
class CoolantOverrideConfig:
    vacuum_mode: str = "default"  # "default", "force_on", "force_off"
    air_assist_mode: str = "default"  # "default", "force_on", "force_off"
    is_air_carve: bool = False  # If True, forces all coolant and vacuum off completely


def apply_coolant_overrides(
    lines: list[GcodeLine],
    config: CoolantOverrideConfig,
) -> list[str]:
    """Filter, replace, or strip M7 (Air/Mist) and M8 (Vacuum/Flood) according to configuration."""
    output_lines: list[str] = []

    # If Air Carve is active, suppress all M7 and M8
    if config.is_air_carve:
        for line in lines:
            # Check if line contains M7 or M8
            has_m7_or_m8 = any(w.letter == "M" and int(w.value) in (7, 8) for w in line.words)
            if not has_m7_or_m8:
                output_lines.append(line.raw_text)
            else:
                # Reconstruct line without M7/M8
                filtered_words = [w.raw_str for w in line.words if not (w.letter == "M" and int(w.value) in (7, 8))]
                if filtered_words:
                    clean = " ".join(filtered_words)
                    comment = f" (Air Carve: suppressed M7/M8 | {line.comment})" if line.comment else " (Air Carve: suppressed M7/M8)"
                    output_lines.append(f"{clean}{comment}")
                else:
                    output_lines.append(f"; (Air Carve: suppressed {line.raw_text})")
        return output_lines

    # Normal mode with optional per-section overrides
    force_on_injected = False

    for line in lines:
        has_m7 = any(w.letter == "M" and int(w.value) == 7 for w in line.words)
        has_m8 = any(w.letter == "M" and int(w.value) == 8 for w in line.words)

        should_strip_m7 = (config.air_assist_mode == "force_off") and has_m7
        should_strip_m8 = (config.vacuum_mode == "force_off") and has_m8

        if should_strip_m7 or should_strip_m8:
            filtered_words = [
                w.raw_str
                for w in line.words
                if not (
                    w.letter == "M"
                    and (
                        (int(w.value) == 7 and should_strip_m7)
                        or (int(w.value) == 8 and should_strip_m8)
                    )
                )
            ]
            if filtered_words:
                clean = " ".join(filtered_words)
                output_lines.append(f"{clean} ({line.comment})" if line.comment else clean)
            else:
                output_lines.append(f"; (Suppressed {line.raw_text})")
            continue

        output_lines.append(line.raw_text)

        # Inject force_on at start of motion if requested and not yet present
        if not force_on_injected and any(w.letter == "G" and int(w.value) in (0, 1, 2, 3) for w in line.words):
            if config.vacuum_mode == "force_on" and not has_m8:
                output_lines.append("M8 (Forced Vacuum ON)")
            if config.air_assist_mode == "force_on" and not has_m7:
                output_lines.append("M7 (Forced Air Assist ON)")
            force_on_injected = True

    return output_lines
