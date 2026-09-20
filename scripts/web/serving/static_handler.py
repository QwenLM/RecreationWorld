import http.server
import os
import posixpath
import sys


class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=sys.argv[2], **kwargs)

    # Genuine static-asset extensions: a MISSING file with one of these 404s;
    # everything else (incl. .html page routes, dotted version dirs like /v1.2/)
    # is treated as a client-side route and falls back to index.html.
    _STATIC_EXT = {
        ".js",
        ".mjs",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".avif",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".map",
        ".json",
        ".xml",
        ".txt",
        ".pdf",
        ".mp4",
        ".webm",
        ".mov",
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".wasm",
        ".zip",
        ".gz",
        ".csv",
    }

    def send_head(self):
        # SPA fallback (nginx `try_files $uri /index.html` semantics): if the
        # requested path has no real file on disk, serve the root index.html for
        # ROUTE-like paths so client-side path routing (BrowserRouter) resolves
        # deep links — including .html page routes (e.g. /docs/x.html) and dotted
        # version dirs (e.g. /v1.2/). Only genuine static-asset extensions 404.
        fs_path = self.translate_path(self.path)
        if not os.path.exists(fs_path):
            route = self.path.split("?", 1)[0].split("#", 1)[0]
            _, ext = posixpath.splitext(route.rstrip("/"))
            if ext.lower() not in self._STATIC_EXT:
                self.path = "/index.html"
        return super().send_head()

    def guess_type(self, path):
        # Force a UTF-8 charset on text responses. Many frozen pages carry `<meta charset>` only
        # AFTER an inlined <style>/<title> block, i.e. beyond Chromium's 1024-byte prescan window,
        # and stdlib guess_type returns "text/html" with no charset — so Chromium falls back to
        # windows-1252 and every non-ASCII byte renders as mojibake (mesos â€™, Â©, Î§
        # anchor glyphs; federalreserve; nps). An explicit HTTP charset beats both meta
        # and prescan, fixing all text pages regardless of where their meta tag sits.
        #
        # Frozen pages may omit the declaration or place it beyond the prescan window.
        # Without an HTTP charset, the agent can browse mojibake while the reference
        # capture renders correctly.
        ctype = super().guess_type(path)
        base = ctype.split(";", 1)[0].strip().lower()
        if (
            base
            in (
                "text/html",
                "text/plain",
                "text/css",
                "application/javascript",
                "text/javascript",
                "application/json",
                "image/svg+xml",
                "text/xml",
                "application/xml",
            )
            and "charset=" not in ctype.lower()
        ):
            return base + "; charset=utf-8"
        return ctype

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress access logs


if __name__ == "__main__":
    port = int(sys.argv[1])
    handler = CORSHandler
    # bind_and_activate happens in ThreadingHTTPServer.__init__; a failed bind (fixed port already
    # taken) raises here and the process exits WITHOUT printing a BOUND_PORT line, which the
    # parent reads as a launch failure. On success we print the ACTUAL bound port: for port 0
    # (ephemeral) this is the OS-assigned, guaranteed-free port that THIS socket now owns and
    # holds for its whole lifetime — the parent uses exactly this port, so there is no
    # pick-a-port-then-bind-it race window for a concurrent pod to slip into.
    # Browsers request HTML, JS, CSS, fonts, and images concurrently.  The plain
    # HTTPServer serializes those requests; on asset-heavy frozen sites one slow
    # response can hold every font request behind it, leaving Playwright's screenshot
    # call stuck at "waiting for fonts" until Chromium kills the renderer.  A threaded
    # server preserves the same static-file semantics while allowing the browser's
    # independent requests to make progress.
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        sys.stdout.write(f"BOUND_PORT {httpd.server_address[1]}\n")
        sys.stdout.flush()
        httpd.serve_forever()
