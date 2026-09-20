"""Recreation-Bench: Benchmark for multimodal coding agents on app recreation tasks.

The public surface is the suite's two contracts -- what a task IS and what a run PRODUCED. The
pipeline that runs them lives in ``scripts/`` and is not importable from here on purpose: it is
delivered to five remote hosts as a tarball and run with ``PYTHONPATH=<root>/scripts``, never
pip-installed, so nothing under ``src/`` is on the path where it executes.
"""

from recreation_bench.result import RunResult, StageStatus
from recreation_bench.task import SCHEMA_VERSION, Platform, TaskInstance, TaskPatch

__version__ = "0.1.0"

__all__ = [
    "Platform",
    "RunResult",
    "SCHEMA_VERSION",
    "StageStatus",
    "TaskInstance",
    "TaskPatch",
    "__version__",
]
