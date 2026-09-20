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
try:
    budget = int(os.environ.get("RB_TIME_BUDGET_SEC", "0"))
except Exception:
    budget = 0
if budget <= 0:
    sys.exit(0)
state_dir = os.environ.get("RB_TIME_HOOK_STATE_DIR") or os.environ.get("TEMP") or "."
path = os.path.join(state_dir, "tb_state_%s.json" % sid)
now = time.time()
try:
    with open(path) as f:
        st = json.load(f)
except Exception:
    st = {"start": now, "fired": []}
elapsed = now - float(st.get("start", now))
frac = elapsed / budget
remain_min = int(max(0, (budget - elapsed)) // 60)
if not st.get("alive"):
    st["alive"] = True
    _log(
        state_dir,
        "CHECK-ALIVE session=%s frac=%.3f remain=%dmin" % (sid, frac, remain_min),
    )
TIERS = [
    (
        0.25,
        "context",
        "Time check: ~25% of your time budget used (~{remain} min left). Keep exploration "
        "efficient; aim to finish understanding the reference app soon and move toward implementation.",
    ),
    (
        0.50,
        "context",
        "Half your time budget is gone (~{remain} min left). You should be writing code now - make "
        "sure you will end up with src/, build.ps1 and launch.ps1, not only exploration notes.",
    ),
    (
        0.75,
        "block",
        "75% of time used (~{remain} min left). IF you have NOT yet produced src/ + "
        "build.ps1 + launch.ps1, stop exploring the reference app now and write a minimal compilable "
        "implementation, then build it once. If you already have them, spend the rest verifying it "
        "builds/launches and refining.",
    ),
    (
        0.90,
        "block",
        "Only ~10% of time left (~{remain} min). src/ + build.ps1 + launch.ps1 are "
        "mandatory or the task scores zero. IF they do not exist yet, STOP everything else and write "
        "whatever compiles right now; otherwise ensure it builds and launches.",
    ),
]
fire = None
for thr, sev, msg in TIERS:
    key = "%.2f" % thr
    if frac >= thr and key not in st.get("fired", []):
        st.setdefault("fired", []).append(key)
        fire = (thr, sev, msg)
try:
    with open(path, "w") as f:
        json.dump(st, f)
except Exception:
    pass
if fire is None:
    sys.exit(0)
thr, sev, msg = fire
text = "[RB TIME BUDGET] " + msg.replace("{remain}", str(remain_min))
mode = os.environ.get("RB_TIME_HOOK_MODE", "").lower()
use_block = (mode == "block") or (mode != "context" and sev == "block")
_log(
    state_dir,
    "FIRE tier=%.2f mode=%s elapsed=%ds frac=%.3f remain=%dmin"
    % (thr, "block" if use_block else "context", int(elapsed), frac, remain_min),
)
if use_block:
    print(json.dumps({"decision": "block", "reason": text}))
else:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": text,
                }
            }
        )
    )
sys.exit(0)
