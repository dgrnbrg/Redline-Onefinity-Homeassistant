"""Configuration and persistent settings for Redline Job Linker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".redline_job_linker"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

DEFAULT_EPILOGUE = """(--- CUSTOM EPILOGUE ---)
M5               ; Spindle Stop
M9               ; Coolant/Vacuum Off
G53 G0 Z0        ; Safe Z retract to machine ceiling
G53 G0 X0 Y800   ; Move gantry out of the way
M30              ; Program End and Reset
"""


@dataclass
class JobLinkerConfig:
    """User and runtime configuration settings."""

    watched_dir: str = ""
    controller_host: str = "192.168.1.150"
    controller_port: int = 8080
    controller_subfolder: str = "gcode/workflow"
    stale_ttl_hours: float = 24.0
    auto_sync_enabled: bool = True
    auto_upload_staged_enabled: bool = True
    auto_sync_idle_seconds: float = 10.0
    air_carve_height_mm: float = 25.0
    spoilboard_max_cut_depth_mm: float = 1.27  # 0.05 inches
    safe_z_command: str = "G53 G0 Z0"
    gantry_park_x: float = 0.0
    gantry_park_y: float = 800.0
    custom_epilogue: str = DEFAULT_EPILOGUE
    spindle_dwell_seconds: float = 3.0
    server_port: int = 8088
    default_wcs_sequence: list[str] = field(
        default_factory=lambda: [
            "G54",
            "G55",
            "G56",
            "G57",
            "G58",
            "G59",
            "G59.1",
            "G59.2",
            "G59.3",
        ]
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobLinkerConfig:
        fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in fields}
        return cls(**filtered)


def load_config(config_path: Path = DEFAULT_CONFIG_FILE) -> JobLinkerConfig:
    """Load configuration from disk or create default."""
    if not config_path.exists():
        cfg = JobLinkerConfig()
        save_config(cfg, config_path)
        return cfg
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JobLinkerConfig.from_dict(data)
    except Exception:
        return JobLinkerConfig()


def save_config(cfg: JobLinkerConfig, config_path: Path = DEFAULT_CONFIG_FILE) -> None:
    """Save configuration to disk atomically."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, indent=2)
    tmp_path.replace(config_path)
