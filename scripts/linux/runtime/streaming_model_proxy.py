#!/usr/bin/env python3
"""Small streaming HTTP relay used by the isolated Linux Codex process."""

from __future__ import annotations

import http.server
import os
import sys
import urllib.error
import urllib.request

UPSTREAM = os.environ["UPSTREAM_BASE"].rstrip("/")
HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def forward(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP
        }
        request = urllib.request.Request(
            UPSTREAM + self.path,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(request, timeout=1800)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:  # noqa: BLE001 - transport errors become HTTP 502
            print(f"CODEX-PROXY-ERR {exc}", file=sys.stderr, flush=True)
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:  # noqa: BLE001 - the client may already be gone
                pass
            return
        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.end_headers()
        try:
            while chunk := response.read(1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:  # noqa: BLE001 - disconnects do not kill the relay
            pass

    do_GET = forward  # noqa: N815 - BaseHTTPRequestHandler callback name
    do_POST = forward  # noqa: N815 - BaseHTTPRequestHandler callback name

    def log_message(self, *args) -> None:
        pass


http.server.ThreadingHTTPServer(
    ("127.0.0.1", int(os.environ["PORT"])), Handler
).serve_forever()
