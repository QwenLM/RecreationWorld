
import json
import os
import subprocess

base = os.environ["RB_INVOCATION_DIR"]
pid_path = os.path.join(base, "pid")
exit_path = os.path.join(base, "exit_code")
out_path = os.path.join(base, "output")

try:
    with open(out_path, errors="replace") as f:
        output = f.read()
except Exception:
    output = ""

if os.path.exists(exit_path):
    try:
        code = int(open(exit_path).read().strip())
    except Exception:
        code = 255
    if code == 0:
        status = "Success"
    elif code == 124:
        status = "Timeout"
    else:
        status = "Failed"
    print(json.dumps({"status": status, "exit_code": code, "output": output}))
    raise SystemExit

running = False
try:
    pid = open(pid_path).read().strip()
    running = bool(pid) and subprocess.run(
        ["kill", "-0", pid],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
except Exception:
    running = False

if running:
    status = "Running"
    code = None
else:
    status = "Failed"
    code = 255
    if not output:
        output = "SSH invocation has no exit_code and no running process"

print(json.dumps({"status": status, "exit_code": code, "output": output}))
