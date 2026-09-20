#!/usr/bin/env python3
"""Small dependency-free browser viewer and input bridge for an ADB device."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse


STATIC_ROOT = Path(__file__).with_name("static")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
KEYCODES = {
    "back": "KEYCODE_BACK",
    "delete": "KEYCODE_DEL",
    "enter": "KEYCODE_ENTER",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
}


class BridgeError(RuntimeError):
    pass


@dataclass
class VideoPipeline:
    source: BinaryIO
    ffmpeg: subprocess.Popen[bytes]
    stopped: threading.Event
    pump: threading.Thread
    processes: list[subprocess.Popen[bytes]]
    source_socket: socket.socket | None = None
    backend: str = "screenrecord"


class AdbBridge:
    def __init__(self, adb: str, serial: str | None) -> None:
        self.adb = adb
        self.requested_serial = serial or ""
        self.frame_lock = threading.Lock()
        self.scrcpy_server = os.environ.get("RB_SCRCPY_SERVER", "")
        self.scrcpy_version = os.environ.get("RB_SCRCPY_VERSION", "4.0")
        self.scrcpy_port = int(os.environ.get("RB_SCRCPY_PORT", "27183"))
        self.scrcpy_pushed_to: set[str] = set()
        self.scrcpy_lock = threading.Lock()

    def _run(self, *args: str, timeout: float = 15, binary: bool = False):
        command = [self.adb]
        serial = self.serial()
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=not binary,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"ADB command failed: {exc}") from exc
        if result.returncode != 0:
            error = result.stderr if isinstance(result.stderr, str) else result.stderr.decode()
            raise BridgeError(error.strip() or f"ADB exited with {result.returncode}")
        return result.stdout

    def serial(self) -> str:
        if self.requested_serial:
            return self.requested_serial
        try:
            result = subprocess.run(
                [self.adb, "devices"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        for line in result.stdout.splitlines()[1:]:
            columns = line.split()
            if len(columns) == 2 and columns[1] == "device":
                return columns[0]
        return ""

    def status(self) -> dict[str, object]:
        serial = self.serial()
        if not serial:
            return {"connected": False, "serial": "", "message": "No ADB device detected"}
        try:
            state = self._run("get-state").strip()
            size_output = self._run("shell", "wm", "size")
            size_match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", size_output)
            model = self._run("shell", "getprop", "ro.product.model").strip()
            api_level = self._run("shell", "getprop", "ro.build.version.sdk").strip()
            focus_output = self._run("shell", "dumpsys", "window", timeout=20)
            focus_match = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9_.]+)/", focus_output)
            return {
                "connected": state == "device",
                "serial": serial,
                "model": model,
                "apiLevel": api_level,
                "width": int(size_match.group(1)) if size_match else None,
                "height": int(size_match.group(2)) if size_match else None,
                "package": focus_match.group(1) if focus_match else "",
            }
        except BridgeError as exc:
            return {"connected": False, "serial": serial, "message": str(exc)}

    def frame(self) -> bytes:
        if not self.serial():
            raise BridgeError("No ADB device detected")
        with self.frame_lock:
            payload = self._run("exec-out", "screencap", "-p", timeout=20, binary=True)
        if not payload.startswith(PNG_SIGNATURE):
            raise BridgeError("ADB did not return a PNG screenshot")
        return payload

    def _scrcpy_source(self, serial: str) -> tuple[BinaryIO, list[subprocess.Popen[bytes]], socket.socket] | None:
        if not self.scrcpy_server:
            return None
        server_path = Path(self.scrcpy_server)
        if not server_path.is_file():
            raise BridgeError(f"scrcpy server not found: {server_path}")
        remote_path = f"/data/local/tmp/scrcpy-server-v{self.scrcpy_version}.jar"
        scid = f"{secrets.randbits(31):08x}"
        with self.scrcpy_lock:
            if serial not in self.scrcpy_pushed_to:
                self._run("push", str(server_path), remote_path, timeout=60)
                self.scrcpy_pushed_to.add(serial)
            try:
                subprocess.run(
                    [
                        self.adb,
                        "-s",
                        serial,
                        "forward",
                        f"tcp:{self.scrcpy_port}",
                        f"localabstract:scrcpy_{scid}",
                    ],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise BridgeError(f"Could not create the scrcpy ADB forward: {exc}") from exc
        server_process = subprocess.Popen(
            [
                self.adb,
                "-s",
                serial,
                "shell",
                f"CLASSPATH={remote_path}",
                "app_process",
                "/",
                "com.genymobile.scrcpy.Server",
                self.scrcpy_version,
                f"scid={scid}",
                "tunnel_forward=true",
                "audio=false",
                "control=false",
                "cleanup=false",
                "raw_stream=true",
                "max_size=960",
                "video_bit_rate=4000000",
                "max_fps=30",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # An ADB forward accepts host connections before the device socket exists.
        # Give app_process time to bind its unique socket so that raw_stream does
        # not appear as an immediate successful connection followed by EOF.
        time.sleep(0.5)
        deadline = time.monotonic() + 5
        source_socket: socket.socket | None = None
        while time.monotonic() < deadline and server_process.poll() is None:
            try:
                source_socket = socket.create_connection(("127.0.0.1", self.scrcpy_port), timeout=0.5)
                source_socket.settimeout(None)
                break
            except OSError:
                time.sleep(0.05)
        if source_socket is None:
            server_process.terminate()
            raise BridgeError("Could not connect to the scrcpy video server")
        return source_socket.makefile("rb", buffering=0), [server_process], source_socket

    def video_pipeline(self) -> VideoPipeline:
        serial = self.serial()
        if not serial:
            raise BridgeError("No ADB device detected")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise BridgeError("ffmpeg is required for the H.264 stream")

        scrcpy_source = self._scrcpy_source(serial)
        if scrcpy_source:
            source, processes, source_socket = scrcpy_source
            backend = "scrcpy"
        else:
            adb_process = subprocess.Popen(
                [
                    self.adb,
                    "-s",
                    serial,
                    "exec-out",
                    "screenrecord",
                    "--output-format=h264",
                    "--size",
                    "720x1600",
                    "--bit-rate",
                    "4000000",
                    "--time-limit",
                    "0",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert adb_process.stdout is not None
            source = adb_process.stdout
            processes = [adb_process]
            source_socket = None
            backend = "screenrecord"
        input_read, input_write = os.pipe()
        try:
            ffmpeg_process = subprocess.Popen(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-fpsprobesize",
                    "0",
                    "-fflags",
                    "+genpts",
                    "-r",
                    "25",
                    "-f",
                    "h264",
                    "-i",
                    "pipe:0",
                    "-c:v",
                    "copy",
                    "-movflags",
                    "empty_moov+default_base_moof+frag_every_frame",
                    "-flush_packets",
                    "1",
                    "-f",
                    "mp4",
                    "pipe:1",
                ],
                stdin=input_read,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            os.close(input_write)
            for process in processes:
                process.terminate()
            raise BridgeError(f"Could not start video stream: {exc}") from exc
        finally:
            os.close(input_read)

        stopped = threading.Event()

        def pump_h264() -> None:
            # Android's encoder can leave a lone IDR frame open while the screen is
            # static. An AUD after an idle gap closes that access unit immediately.
            access_unit_delimiter = b"\x00\x00\x00\x01\x09\xf0"
            pending_boundary = False
            try:
                while not stopped.is_set():
                    readable, _, _ = select.select([source], [], [], 0.1)
                    if readable:
                        chunk = os.read(source.fileno(), 64 * 1024)
                        if not chunk:
                            break
                        os.write(input_write, chunk)
                        pending_boundary = True
                    elif pending_boundary:
                        os.write(input_write, access_unit_delimiter)
                        pending_boundary = False
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    os.close(input_write)
                except OSError:
                    pass

        pump_thread = threading.Thread(target=pump_h264, name="adb-h264-pump", daemon=True)
        pump_thread.start()
        return VideoPipeline(
            source=source,
            ffmpeg=ffmpeg_process,
            stopped=stopped,
            pump=pump_thread,
            processes=processes,
            source_socket=source_socket,
            backend=backend,
        )

    def packages(self) -> list[str]:
        output = self._run("shell", "pm", "list", "packages", "-3", timeout=20)
        return sorted(line.removeprefix("package:").strip() for line in output.splitlines())

    @staticmethod
    def _coordinate(value: object) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BridgeError("Coordinates must be numbers")
        rounded = round(value)
        if rounded < 0 or rounded > 10000:
            raise BridgeError("Coordinate is outside the supported range")
        return str(rounded)

    def input(self, payload: dict[str, object]) -> None:
        action = payload.get("action")
        if action == "tap":
            self._run(
                "shell",
                "input",
                "tap",
                self._coordinate(payload.get("x")),
                self._coordinate(payload.get("y")),
            )
            return
        if action == "swipe":
            duration = payload.get("duration", 300)
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise BridgeError("Swipe duration must be a number")
            duration = max(80, min(round(duration), 2000))
            self._run(
                "shell",
                "input",
                "swipe",
                self._coordinate(payload.get("x1")),
                self._coordinate(payload.get("y1")),
                self._coordinate(payload.get("x2")),
                self._coordinate(payload.get("y2")),
                str(duration),
            )
            return
        if action == "key":
            key = payload.get("key")
            if not isinstance(key, str) or key not in KEYCODES:
                raise BridgeError("Unsupported key")
            self._run("shell", "input", "keyevent", KEYCODES[key])
            return
        if action == "text":
            value = payload.get("text")
            if not isinstance(value, str) or len(value) > 500:
                raise BridgeError("Text must contain at most 500 characters")
            encoded = value.replace(" ", "%s")
            self._run("shell", f"input text {shlex.quote(encoded)}")
            return
        if action == "launch":
            package = payload.get("package")
            if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
                raise BridgeError("Invalid Android package name")
            self._run(
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
                timeout=20,
            )
            return
        raise BridgeError("Unsupported input action")


class FrameCache:
    def __init__(self, bridge: AdbBridge, target_fps: float) -> None:
        self.bridge = bridge
        self.interval = 1 / target_fps
        self.condition = threading.Condition()
        self.payload: bytes | None = None
        self.error: str | None = None
        self.generation = 0
        self.captured_at = 0.0
        self.capture_times: deque[float] = deque(maxlen=30)
        self.active_video_streams = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._capture_loop, name="adb-frame-cache", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=2)

    def video_started(self) -> None:
        with self.condition:
            self.active_video_streams += 1
            self.condition.notify_all()

    def video_stopped(self) -> None:
        with self.condition:
            self.active_video_streams = max(0, self.active_video_streams - 1)
            self.condition.notify_all()

    def _capture_loop(self) -> None:
        while not self.stopped.is_set():
            with self.condition:
                if self.active_video_streams:
                    self.condition.wait(timeout=0.5)
                    continue
            started = time.monotonic()
            try:
                payload = self.bridge.frame()
                captured_at = time.monotonic()
                with self.condition:
                    self.payload = payload
                    self.error = None
                    self.generation += 1
                    self.captured_at = captured_at
                    self.capture_times.append(captured_at)
                    self.condition.notify_all()
            except BridgeError as exc:
                with self.condition:
                    self.error = str(exc)
                    self.condition.notify_all()
            remaining = self.interval - (time.monotonic() - started)
            self.stopped.wait(max(0.01, remaining))

    def frame(self, timeout: float = 20) -> tuple[bytes, int]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.payload is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError(self.error or "Timed out waiting for an Android frame")
                self.condition.wait(remaining)
            return self.payload, self.generation

    def stats(self) -> dict[str, object]:
        with self.condition:
            if len(self.capture_times) > 1:
                elapsed = self.capture_times[-1] - self.capture_times[0]
                fps = (len(self.capture_times) - 1) / elapsed if elapsed > 0 else 0.0
            else:
                fps = 0.0
            age_ms = round((time.monotonic() - self.captured_at) * 1000) if self.captured_at else None
            return {
                "streamFps": round(fps, 1),
                "frameAgeMs": age_ms,
                "streamError": self.error or "",
            }


class ResultStore:
    def __init__(self, run_dir: Path | None, runs_root: Path, task_id: str) -> None:
        self.run_dir = run_dir
        self.runs_root = runs_root
        self.task_id = task_id

    def _active_run_dir(self) -> Path | None:
        if self.run_dir is not None:
            return self.run_dir
        if not self.runs_root.is_dir():
            return None
        metrics_files = list(self.runs_root.glob("*/metrics.json"))
        if not metrics_files:
            return None
        return max(metrics_files, key=lambda path: path.stat().st_mtime).parent

    def _metrics(self) -> tuple[Path | None, dict[str, object]]:
        run_dir = self._active_run_dir()
        metrics_path = run_dir / "metrics.json" if run_dir else None
        if metrics_path is None or not metrics_path.is_file():
            return metrics_path, {}
        try:
            value = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return metrics_path, {}
        return metrics_path, value if isinstance(value, dict) else {}

    def summary(self) -> dict[str, object]:
        metrics_path, metrics = self._metrics()
        run_dir = metrics_path.parent if metrics_path else self._active_run_dir()
        task_id = str(metrics.get("task_id") or self.task_id or "")
        return {
            "taskId": task_id,
            "runName": run_dir.name if run_dir else "",
            "stage": metrics.get("stage") or "running",
            "evalStatus": metrics.get("eval_status") or "",
            "taskScore": metrics.get("task_score"),
            "programScore": metrics.get("program_score"),
            "vlmScore": metrics.get("vlm_score"),
            "passed": metrics.get("passed"),
            "metricsAvailable": bool(metrics_path and metrics_path.is_file()),
        }

    def metrics_bytes(self) -> bytes | None:
        metrics_path, _ = self._metrics()
        if metrics_path is None or not metrics_path.is_file():
            return None
        try:
            return metrics_path.read_bytes()
        except OSError:
            return None


class ViewerHandler(BaseHTTPRequestHandler):
    server: "ViewerServer"

    def log_message(self, message: str, *args: object) -> None:
        print(f"[viewer] {self.address_string()} {message % args}")

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._json({"ok": True})
                return
            if path == "/api/status":
                status = self.server.bridge.status()
                status.update(self.server.frames.stats())
                self._json(status)
                return
            if path == "/api/packages":
                self._json({"packages": self.server.bridge.packages()})
                return
            if path == "/api/result":
                self._json(self.server.results.summary())
                return
            if path == "/metrics.json":
                body = self.server.results.metrics_bytes()
                if body is None:
                    self._error(HTTPStatus.NOT_FOUND, "Metrics are not available")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/frame.png":
                frame, generation = self.server.frames.frame()
                etag = f'"frame-{generation}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-store, max-age=0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(frame)
                return
            if path == "/api/stream.mp4":
                self._stream_video()
                return
            self._serve_static(path)
        except BridgeError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except BrokenPipeError:
            pass

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _stream_video(self) -> None:
        with self.server.video_lock:
            pipeline = self.server.bridge.video_pipeline()
            self.server.frames.video_started()
            assert pipeline.ffmpeg.stdout is not None
            print(f"[viewer] video backend={pipeline.backend}", flush=True)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    readable, _, _ = select.select([pipeline.ffmpeg.stdout, self.connection], [], [], 1)
                    if self.connection in readable:
                        try:
                            if not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                                break
                        except BlockingIOError:
                            pass
                    if pipeline.ffmpeg.stdout not in readable:
                        continue
                    chunk = os.read(pipeline.ffmpeg.stdout.fileno(), 16 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                pipeline.ffmpeg.stdout.close()
                pipeline.stopped.set()
                self._stop_process(pipeline.ffmpeg)
                for process in pipeline.processes:
                    self._stop_process(process)
                pipeline.pump.join(timeout=2)
                pipeline.source.close()
                if pipeline.source_socket is not None:
                    pipeline.source_socket.close()
                self.server.frames.video_stopped()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/input":
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise BridgeError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeError("Expected a JSON object")
            print(f"[viewer] input action={payload.get('action', '<missing>')}", flush=True)
            self.server.bridge.input(payload)
            self._json({"ok": True})
        except (BridgeError, json.JSONDecodeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        candidate = (STATIC_ROOT / relative).resolve()
        try:
            candidate.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        bridge: AdbBridge,
        target_fps: float,
        results: ResultStore,
    ) -> None:
        self.bridge = bridge
        self.frames = FrameCache(bridge, target_fps)
        self.results = results
        self.video_lock = threading.Lock()
        super().__init__(address, ViewerHandler)
        self.frames.start()

    def server_close(self) -> None:
        self.frames.stop()
        super().server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("RB_VIEWER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RB_VIEWER_PORT", "8080")))
    parser.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--run-dir", default=os.environ.get("RB_RUN_DIR", ""))
    parser.add_argument(
        "--runs-root",
        default=os.environ.get("RB_RUNS_ROOT", "/opt/recreationbench/runs"),
    )
    parser.add_argument("--task-id", default=os.environ.get("RB_TASK_ID", ""))
    parser.add_argument(
        "--fps",
        type=float,
        default=float(os.environ.get("RB_VIEWER_FPS", "6")),
        help="target ADB screenshot capture rate (1-12)",
    )
    args = parser.parse_args()
    if not 1 <= args.fps <= 12:
        parser.error("--fps must be between 1 and 12")

    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else None
    results = ResultStore(run_dir, Path(args.runs_root).expanduser().resolve(), args.task_id)
    server = ViewerServer(
        (args.host, args.port),
        AdbBridge(args.adb, args.serial),
        args.fps,
        results,
    )
    print(f"[viewer] listening on http://{args.host}:{args.port} target_fps={args.fps:g}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
