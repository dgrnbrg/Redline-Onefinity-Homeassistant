"""Redline Job Linker and Multi-Fixture Workflow Batcher."""

from .config import JobLinkerConfig, load_config, save_config
from .engine.assembler import AssemblyOutput, FixtureSlotConfig, assemble_workflow
from .engine.job_grouper import LogicalJob, group_files_into_logical_jobs
from .engine.parser import ParsedJob, parse_gcode
from .engine.z_detector import ZOriginAnalysis, detect_z_origin, shift_line_z, shift_lines_z

__version__ = "1.0.0"

__all__ = [
    "JobLinkerConfig",
    "load_config",
    "save_config",
    "AssemblyOutput",
    "FixtureSlotConfig",
    "assemble_workflow",
    "LogicalJob",
    "group_files_into_logical_jobs",
    "ParsedJob",
    "parse_gcode",
    "ZOriginAnalysis",
    "detect_z_origin",
    "shift_line_z",
    "shift_lines_z",
]
