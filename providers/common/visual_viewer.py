"""Local noVNC viewer generation for RecreationBench sandboxes."""

from __future__ import annotations

import html
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote


def sandbox_novnc_url(host: str, *, https: bool = True) -> str:
    scheme = "https" if https else "http"
    return f"{scheme}://{host}/vnc.html?autoconnect=1&resize=scale"


def sandbox_websocket_url(host: str, *, secure: bool = True) -> str:
    scheme = "wss" if secure else "ws"
    return f"{scheme}://{host}/websockify"


def local_novnc_command(host: str, password: str, task_id: str, *, port: int = 8771) -> str:
    parts = [
        "python",
        "-m",
        "common.secure_novnc_server",
        "--host",
        host,
        "--password",
        password,
        "--task-id",
        task_id,
        "--port",
        str(port),
    ]
    return " ".join(shlex.quote(part) for part in parts)


def sandbox_proxy_tokens(sandbox: object) -> tuple[str | None, str | None]:
    """Return FC dynamic-port credentials without requiring SDK internals elsewhere."""

    envd_token = getattr(sandbox, "_envd_access_token", None)
    traffic_token = getattr(sandbox, "traffic_access_token", None)
    if not isinstance(envd_token, str) or not envd_token:
        envd_token = None
    if not isinstance(traffic_token, str) or not traffic_token:
        traffic_token = None
    return envd_token, traffic_token


def package_name_from_task_id(task_id: str) -> str:
    package = re.sub(r"^bench\d+-", "", task_id.strip())
    return package or task_id


def _load_json_text(value: str | bytes | None) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _format_score(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, int | float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _format_status(value: object) -> str:
    text = str(value or "running").strip()
    labels = {
        "visual_only": "Visual only",
        "recreation_eval": "Evaluated",
        "not_evaluated": "Not evaluated",
        "failed": "Failed",
        "ready": "Ready",
        "running": "Running",
    }
    return labels.get(text, text.replace("_", " ").capitalize())


def write_visual_index(
    result_dir: Path,
    *,
    task_id: str,
    platform: str,
    viewer_url: str | None = None,
    sandbox_id: str = "",
    metrics: str | bytes | None = None,
) -> Path:
    """Write a single-app visual result page."""

    result_dir.mkdir(parents=True, exist_ok=True)
    if viewer_url is None:
        for url_path in (result_dir / "local_novnc.url", result_dir / "local_viewer.url"):
            if url_path.is_file():
                viewer_url = url_path.read_text(encoding="utf-8", errors="replace").strip()
                break
    metrics_payload = _load_json_text(metrics)
    if not metrics_payload:
        metrics_path = result_dir / "metrics.json"
        if metrics_path.is_file():
            metrics_payload = _load_json_text(metrics_path.read_text(encoding="utf-8", errors="replace"))

    package_name = package_name_from_task_id(task_id)
    title = f"RecreationBench - {package_name}"
    score = metrics_payload.get("task_score")
    if score is None:
        score = metrics_payload.get("program_score")
    status = metrics_payload.get("stage") or metrics_payload.get("eval_status") or "running"
    passed = metrics_payload.get("passed")
    score_text = _format_score(score)
    passed_text = "-" if passed is None else str(bool(passed)).lower()
    status_text = _format_status(status)
    iframe = (
        f'<iframe title="{html.escape(package_name)} desktop" src="{html.escape(viewer_url)}"></iframe>'
        if viewer_url
        else '<div class="empty"><strong>Viewer unavailable</strong><span>Run with --visual-only or --visual.</span></div>'
    )
    viewer_link = (
        f'<a class="button primary" href="{html.escape(viewer_url)}" target="_blank" rel="noreferrer">Open viewer</a>'
        if viewer_url
        else '<span class="button disabled">Open viewer</span>'
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #111827;
      --panel-2: #172033;
      --border: #293548;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --blue: #60a5fa;
      --green: #34d399;
      --yellow: #fbbf24;
    }}
    html, body {{
      height: 100%;
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body {{
      display: grid;
      grid-template-rows: 58px 1fr;
      min-width: 900px;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 0 18px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }}
    .title {{
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      line-height: 1;
    }}
    .subtitle {{
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 0 0 auto;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 30px;
      padding: 0 11px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
      text-decoration: none;
    }}
    .button.primary {{
      border-color: #2563eb;
      background: #1d4ed8;
    }}
    .button.disabled {{
      color: var(--muted);
      cursor: default;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      min-height: 0;
    }}
    .viewer {{
      position: relative;
      min-width: 0;
      min-height: 0;
      background: #020617;
    }}
    iframe {{
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
      background: #020617;
    }}
    .viewer-bar {{
      position: absolute;
      left: 14px;
      top: 14px;
      z-index: 1;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border: 1px solid rgb(148 163 184 / 20%);
      border-radius: 6px;
      background: rgb(15 23 42 / 86%);
      color: #cbd5e1;
      font-size: 12px;
      pointer-events: none;
    }}
    .dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 3px rgb(52 211 153 / 15%);
    }}
    aside {{
      overflow: auto;
      border-left: 1px solid var(--border);
      background: var(--panel);
      padding: 16px;
    }}
    .summary {{
      display: grid;
      gap: 12px;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    .stat {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-2);
      padding: 11px;
    }}
    .stat label {{
      display: block;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .stat strong {{
      display: block;
      overflow-wrap: anywhere;
      font-size: 14px;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      width: fit-content;
      padding: 5px 8px;
      border: 1px solid rgb(52 211 153 / 30%);
      border-radius: 999px;
      background: rgb(6 78 59 / 35%);
      color: #bbf7d0;
      font-size: 12px;
    }}
    dl {{
      display: grid;
      grid-template-columns: 82px 1fr;
      gap: 9px 10px;
      margin: 0;
      font-size: 12px;
    }}
    dt {{ color: var(--muted); }}
    dd {{
      margin: 0;
      overflow-wrap: anywhere;
    }}
    a {{ color: #93c5fd; }}
    .empty {{
      display: grid;
      place-items: center;
      height: 100%;
      color: var(--muted);
    }}
    .empty span {{
      display: block;
      margin-top: 6px;
      font-size: 12px;
    }}
    @media (max-width: 980px) {{
      body {{
        min-width: 0;
        grid-template-rows: auto 1fr;
      }}
      header {{
        align-items: flex-start;
        flex-direction: column;
        padding: 12px;
      }}
      main {{
        grid-template-columns: 1fr;
        grid-template-rows: minmax(520px, 70vh) auto;
      }}
      aside {{
        border-left: 0;
        border-top: 1px solid var(--border);
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="title">
      <h1>{html.escape(package_name)}</h1>
      <span class="subtitle">{html.escape(platform)} / {html.escape(task_id)}</span>
    </div>
    <nav class="actions" aria-label="Result actions">
      {viewer_link}
      <a class="button" href="metrics.json" target="_blank" rel="noreferrer">Metrics</a>
    </nav>
  </header>
  <main>
    <section class="viewer"><div class="viewer-bar"><span class="dot"></span><span>Live desktop</span></div>{iframe}</section>
    <aside>
      <section class="summary">
        <div class="status-pill"><span class="dot"></span>{html.escape(status_text)}</div>
        <div class="stat-grid">
          <div class="stat"><label>Score</label><strong>{html.escape(score_text)}</strong></div>
          <div class="stat"><label>Passed</label><strong>{html.escape(passed_text)}</strong></div>
        </div>
        <dl>
          <dt>Package</dt><dd>{html.escape(package_name)}</dd>
          <dt>Sandbox</dt><dd>{html.escape(sandbox_id or "-")}</dd>
          <dt>Viewer</dt><dd>{'<a href="' + html.escape(viewer_url) + '" target="_blank" rel="noreferrer">open in new tab</a>' if viewer_url else '-'}</dd>
          <dt>Metrics</dt><dd><a href="metrics.json" target="_blank" rel="noreferrer">metrics.json</a></dd>
        </dl>
      </section>
    </aside>
  </main>
</body>
</html>
"""
    path = result_dir / "visual_index.html"
    path.write_text(body, encoding="utf-8")
    return path


def start_local_novnc_server(
    *,
    host: str,
    password: str,
    task_id: str,
    result_dir: Path,
    platform: str = "",
    sandbox_id: str = "",
    port: int = 0,
    timeout_sec: float = 30.0,
    envd_access_token: str | None = None,
    traffic_access_token: str | None = None,
) -> str:
    """Start the loopback noVNC proxy and return the checked local URL."""

    result_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "local_viewer.log"
    pid_path = result_dir / "local_viewer.pid"
    args = [
        sys.executable,
        "-m",
        "common.secure_novnc_server",
        "--host",
        host,
        "--password",
        password,
        "--task-id",
        task_id,
        "--port",
        str(port),
        "--index-file",
        str(result_dir / "visual_index.html"),
    ]
    child_env = os.environ.copy()
    if envd_access_token:
        child_env["RB_VIEWER_ENVD_ACCESS_TOKEN"] = envd_access_token
    else:
        child_env.pop("RB_VIEWER_ENVD_ACCESS_TOKEN", None)
    if traffic_access_token:
        child_env["RB_VIEWER_TRAFFIC_ACCESS_TOKEN"] = traffic_access_token
    else:
        child_env.pop("RB_VIEWER_TRAFFIC_ACCESS_TOKEN", None)
    log = log_path.open("ab")
    proc = subprocess.Popen(
        args,
        cwd=Path(__file__).resolve().parents[1],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        start_new_session=True,
    )
    pid_path.write_text(f"{proc.pid}\n")
    deadline = time.monotonic() + timeout_sec
    line = ""
    while time.monotonic() < deadline:
        if proc.stdout is not None and select.select([proc.stdout], [], [], 0.1)[0]:
            line = proc.stdout.readline().strip()
            if line:
                break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if not line.startswith("visual_local_server="):
        proc.poll()
        stderr = ""
        try:
            stderr = log_path.read_text(errors="replace")[-4000:]
        except Exception:
            pass
        raise RuntimeError(
            "local_viewer_failed: expected visual_local_server from proxy; "
            f"exit_code={proc.returncode} log={log_path} detail={stderr.strip()}"
        )
    url = line.split("=", 1)[1]
    origin = url.rstrip("/")
    local_novnc_url = f"{origin}/vnc.html?autoconnect=true&resize=scale&password={quote(password, safe='')}"
    (result_dir / "local_viewer.url").write_text(f"{url}\n")
    (result_dir / "local_novnc.url").write_text(f"{local_novnc_url}\n")
    write_visual_index(
        result_dir,
        task_id=task_id,
        platform=platform,
        sandbox_id=sandbox_id,
        viewer_url=local_novnc_url,
    )
    return url


def write_visual_viewer(
    path: Path,
    *,
    task_id: str,
    platform: str,
    sandbox_id: str,
    host: str,
    password: str,
) -> Path:
    """Write a local visual instruction page.

    The reliable viewer is the loopback proxy in common.secure_novnc_server.
    Keeping this file as an instruction artifact is useful for result bundles,
    but it intentionally does not import noVNC from a CDN or directly from the
    sandbox host.
    """

    ws_url = sandbox_websocket_url(host)
    raw_url = sandbox_novnc_url(host)
    title = f"RecreationBench visual viewer - {task_id}"
    config = {
        "taskId": task_id,
        "platform": platform,
        "sandboxId": sandbox_id,
        "host": host,
        "wsUrl": ws_url,
        "rawUrl": raw_url,
        "password": password,
    }
    config_json = json.dumps(config, ensure_ascii=False)
    escaped_title = html.escape(title)
    escaped_task = html.escape(task_id)
    escaped_platform = html.escape(platform)
    escaped_sandbox = html.escape(sandbox_id)
    escaped_ws = html.escape(ws_url)
    escaped_raw = html.escape(raw_url)
    command = local_novnc_command(host, password, task_id)
    escaped_command = html.escape(command)

    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    html, body {{
      height: 100%;
      margin: 0;
      background: #111827;
      color: #e5e7eb;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    #toolbar {{
      box-sizing: border-box;
      height: 44px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 12px;
      background: #0f172a;
      border-bottom: 1px solid #334155;
      font-size: 13px;
      white-space: nowrap;
    }}
    #toolbar code {{
      color: #bfdbfe;
      background: #1e293b;
      padding: 2px 5px;
      border-radius: 4px;
    }}
    main {{
      padding: 24px;
      line-height: 1.5;
    }}
    pre {{
      white-space: pre-wrap;
      background: #020617;
      color: #bfdbfe;
      padding: 12px;
      border-radius: 6px;
      border: 1px solid #334155;
    }}
    #status {{
      margin-left: auto;
      color: #fde68a;
    }}
    a {{ color: #93c5fd; }}
  </style>
</head>
<body>
  <div id="toolbar">
    <span>{escaped_platform}</span>
    <code>{escaped_task}</code>
    <span>sandbox <code>{escaped_sandbox}</code></span>
    <span>ws <code>{escaped_ws}</code></span>
    <a href="{escaped_raw}" target="_blank" rel="noreferrer">raw noVNC</a>
    <span id="status">use local proxy</span>
  </div>
  <main>
    <p>Do not open the sandbox-hosted <code>vnc.html</code> directly. FC may serve it as an attachment.</p>
    <p>Start the loopback proxy from <code>recreationbench_service</code>:</p>
    <pre>{escaped_command}</pre>
    <p>Then open <code>http://127.0.0.1:8771/</code>.</p>
    <p>Raw sandbox noVNC: <a href="{escaped_raw}" target="_blank" rel="noreferrer">{escaped_raw}</a></p>
    <p>Sandbox websocket: <code>{escaped_ws}</code></p>
    <script type="application/json" id="visual-config">{config_json}</script>
  </main>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path
