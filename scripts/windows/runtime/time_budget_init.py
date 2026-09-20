#!/usr/bin/env python3
import json
import os
import sys
import time


def _log(state_dir, line):
    try:
        with open(os.path.join(state_dir, "tb_hook.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " " + line + "\n")
    except Exception:
        pass


try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
sid = str(data.get("session_id", "default"))
state_dir = os.environ.get("RB_TIME_HOOK_STATE_DIR") or os.environ.get("TEMP") or "."
budget = os.environ.get("RB_TIME_BUDGET_SEC", "?")
path = os.path.join(state_dir, "tb_state_%s.json" % sid)
resume = os.path.exists(path)
if not resume:
    try:
        with open(path, "w") as f:
            json.dump({"start": time.time(), "fired": []}, f)
    except Exception:
        pass
_log(
    state_dir,
    "%s session=%s budget=%ss" % ("INIT(resume)" if resume else "INIT", sid, budget),
)
sys.exit(0)
