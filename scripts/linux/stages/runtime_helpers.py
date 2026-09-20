"""Linux runtime helper files uploaded beside the stage entrypoint."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import runtime_assets

RUNTIME_HELPER_FILES = {
    "rb_runtime_helpers.sh": runtime_assets.load_text(
        "linux/runtime/runtime_helpers.sh"
    ),
    "trajectory_summary.py": runtime_assets.load_text(
        "linux/runtime/trajectory_summary.py"
    ),
    "trajectory_audit.py": runtime_assets.load_text(
        "linux/runtime/trajectory_audit.py"
    ),
    "cua_mcp_preflight.py": runtime_assets.load_text(
        "linux/runtime/cua_mcp_preflight.py"
    ),
    "streaming_model_proxy.py": runtime_assets.load_text(
        "linux/runtime/streaming_model_proxy.py"
    ),
    "recreation_contract.py": runtime_assets.load_text(
        "linux/runtime/recreation_contract.py"
    ),
}

# Compatibility for tests and out-of-tree callers that imported the old scalar.
RUNTIME_HELPERS_SH = RUNTIME_HELPER_FILES["rb_runtime_helpers.sh"]
