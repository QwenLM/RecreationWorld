"""Authenticated loopback noVNC proxy for RecreationBench visual inspection."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import html
import ipaddress
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote_to_bytes

import aiohttp
from aiohttp import web

DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
MAX_BODY_BYTES = 4 * 1024 * 1024
SESSION_COOKIE = "rb_novnc_session"
SESSION_MAX_AGE_SEC = 12 * 60 * 60
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_connect=10, sock_read=20)
FORWARDED_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Encoding",
    "Cache-Control",
    "ETag",
    "Last-Modified",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def validate_host(raw_host: str) -> str:
    """Validate the E2B 6080 host and return a canonical authority."""

    if not raw_host or any(
        not character.isprintable() or (character.isascii() and character.isspace())
        for character in raw_host
    ):
        raise ValueError("invalid noVNC upstream host")
    if any(character in raw_host for character in ("/", "?", "#", "@", "%", "\\")):
        raise ValueError("invalid noVNC upstream host")

    host = raw_host
    port_text: str | None = None
    if host.startswith("["):
        closing = host.find("]")
        if closing <= 1:
            raise ValueError("invalid noVNC upstream host")
        address_text = host[1:closing]
        suffix = host[closing + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                raise ValueError("invalid noVNC upstream host")
            port_text = suffix[1:]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            raise ValueError("invalid noVNC upstream host") from None
        if not isinstance(address, ipaddress.IPv6Address):
            raise ValueError("invalid noVNC upstream host")
        canonical_host = f"[{address.compressed}]"
    else:
        if host.count(":") > 1 or "[" in host or "]" in host:
            raise ValueError("invalid noVNC upstream host")
        if ":" in host:
            host, port_text = host.rsplit(":", 1)
        if not host or not host.isascii():
            raise ValueError("invalid noVNC upstream host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if len(host) > 253 or any(DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
                raise ValueError("invalid noVNC upstream host") from None
            canonical_host = host.lower()
        else:
            if not isinstance(address, ipaddress.IPv4Address):
                raise ValueError("invalid noVNC upstream host")
            canonical_host = str(address)

    if port_text is None:
        return canonical_host
    if not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("invalid noVNC upstream host")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid noVNC upstream host")
    return f"{canonical_host}:{port}"


@dataclass(frozen=True)
class ViewerConfig:
    host: str
    password: str = field(repr=False)
    task_id: str
    envd_access_token: str | None = field(default=None, repr=False)
    traffic_access_token: str | None = field(default=None, repr=False)
    html_path: Path = field(default_factory=lambda: Path(__file__).with_name("novnc_viewer.html"))
    index_path: Path | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        *,
        host: str,
        password: str,
        task_id: str,
        envd_access_token: str | None,
        traffic_access_token: str | None,
        index_path: Path | None = None,
    ) -> "ViewerConfig":
        validated_host = validate_host(host)
        if not password:
            raise ValueError("missing VNC password")
        if envd_access_token == "" or traffic_access_token == "":
            raise ValueError("empty sandbox access token")
        html_path = Path(__file__).with_name("novnc_viewer.html").resolve(strict=True)
        if not html_path.is_file():
            raise ValueError("viewer HTML is unavailable")
        return cls(
            host=validated_host,
            password=password,
            task_id=task_id,
            envd_access_token=envd_access_token,
            traffic_access_token=traffic_access_token,
            html_path=html_path,
            index_path=index_path,
        )

    @property
    def upstream_origin(self) -> str:
        return f"https://{self.host}"


@dataclass
class Runtime:
    config: ViewerConfig
    ticket: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    session_id: str | None = field(default=None, repr=False)
    local_origin: str | None = None
    viewer_active: bool = False


RUNTIME_KEY: web.AppKey[Runtime] = web.AppKey("runtime", Runtime)


def upstream_headers(runtime: Runtime) -> dict[str, str]:
    headers: dict[str, str] = {}
    if runtime.config.envd_access_token:
        headers["X-Access-Token"] = runtime.config.envd_access_token
    if runtime.config.traffic_access_token:
        headers["e2b-traffic-access-token"] = runtime.config.traffic_access_token
    return headers


async def reject_redirect(
    _session: aiohttp.ClientSession,
    _context: object,
    _params: object,
) -> None:
    raise RuntimeError("upstream redirect rejected")


def client_session(*, cookie_jar: aiohttp.AbstractCookieJar | None = None) -> aiohttp.ClientSession:
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=True),
        timeout=HTTP_TIMEOUT,
        trace_configs=(trace,),
        cookie_jar=cookie_jar,
    )


async def read_bounded(response: aiohttp.ClientResponse) -> bytes:
    if response.content_length is not None and response.content_length > MAX_BODY_BYTES:
        raise ValueError("upstream response is too large")
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("upstream response is too large")
    return bytes(body)


def is_local_origin(request: web.Request, runtime: Runtime) -> bool:
    return (
        request.remote == "127.0.0.1"
        and runtime.local_origin is not None
        and request.headers.get("Origin") == runtime.local_origin
    )


def is_loopback_request(request: web.Request, runtime: Runtime) -> bool:
    return (
        request.remote == "127.0.0.1"
        and runtime.local_origin is not None
        and request.host == runtime.local_origin.removeprefix("http://")
    )


def has_session(request: web.Request, runtime: Runtime) -> bool:
    supplied = request.cookies.get(SESSION_COOKIE)
    origin = request.headers.get("Origin")
    return (
        is_loopback_request(request, runtime)
        and (origin is None or origin == runtime.local_origin)
        and supplied is not None
        and runtime.session_id is not None
        and hmac.compare_digest(supplied, runtime.session_id)
    )


def is_authorized_vnc_entry(request: web.Request, runtime: Runtime) -> bool:
    return (
        is_loopback_request(request, runtime)
        and request.path == "/vnc.html"
        and hmac.compare_digest(request.query.get("password", ""), runtime.config.password)
    )


def set_session_cookie(response: web.StreamResponse, runtime: Runtime) -> None:
    runtime.session_id = secrets.token_urlsafe(32)
    response.set_cookie(
        SESSION_COOKIE,
        runtime.session_id,
        httponly=True,
        samesite="Strict",
        path="/",
        max_age=SESSION_MAX_AGE_SEC,
    )


def plain_response(status: int, text: str) -> web.Response:
    response = web.Response(status=status, text=text)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def local_vnc_path(runtime: Runtime) -> str:
    password = quote(runtime.config.password, safe="")
    return f"/vnc.html?autoconnect=true&resize=scale&password={password}"


async def serve_html(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if runtime.config.index_path is not None:
        try:
            body = runtime.config.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return plain_response(503, "visual index is not ready")
        except (OSError, UnicodeError):
            return plain_response(500, "visual index is unavailable")
        response = web.Response(text=body, content_type="text/html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    raise web.HTTPFound(local_vnc_path(runtime))


async def serve_embedded_html(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    nonce = secrets.token_urlsafe(24)
    try:
        template = runtime.config.html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return plain_response(500, "viewer unavailable")
    body = template.replace("__CSP_NONCE__", nonce).replace(
        "__TASK_ID__", html.escape(runtime.config.task_id)
    )
    response = web.Response(text=body, content_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def exchange_session(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if has_session(request, runtime):
        response = web.json_response({"password": runtime.config.password})
        response.headers["Cache-Control"] = "no-store"
        log("session_reused")
        return response
    if not is_local_origin(request, runtime):
        log("session_rejected reason=origin_or_expired")
        return plain_response(403, "Forbidden")
    try:
        payload = await request.json()
    except Exception:
        log("session_rejected reason=invalid_json")
        return plain_response(403, "Forbidden")
    supplied = payload.get("ticket") if isinstance(payload, dict) else None
    if supplied and (not isinstance(supplied, str) or not hmac.compare_digest(supplied, runtime.ticket)):
        log("session_rejected reason=invalid_ticket")
        return plain_response(403, "Forbidden")

    response = web.json_response({"password": runtime.config.password})
    response.headers["Cache-Control"] = "no-store"
    set_session_cookie(response, runtime)
    log("session_created")
    return response


def validated_asset_path(request: web.Request) -> str | None:
    if request.query_string:
        return None
    raw_path = request.raw_path.partition("?")[0]
    prefix = "/novnc/"
    if not raw_path.startswith(prefix):
        return None
    try:
        decoded = unquote_to_bytes(raw_path[len(prefix) :]).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    if not decoded or any(value in decoded for value in ("\0", "\\", "%", "//")):
        return None
    path = PurePosixPath(decoded)
    if str(path) != decoded or any(part in {"", ".", ".."} for part in decoded.split("/")):
        return None
    if path.suffix != ".js" or not decoded.startswith(("core/", "vendor/pako/")):
        return None
    return decoded


async def proxy_javascript(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if not has_session(request, runtime):
        return plain_response(403, "Forbidden")
    path = validated_asset_path(request)
    if path is None:
        return plain_response(404, "Not Found")
    try:
        async with client_session() as client:
            async with client.get(
                f"{runtime.config.upstream_origin}/{path}",
                headers=upstream_headers(runtime),
                allow_redirects=False,
            ) as upstream:
                if upstream.status != 200:
                    log(f"asset_failed path={path} status={upstream.status}")
                    return plain_response(502, "Bad Gateway")
                body = await read_bounded(upstream)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
        log(f"asset_failed path={path} error={type(exc).__name__}: {exc}")
        return plain_response(502, "Bad Gateway")

    log(f"asset_ok path={path} bytes={len(body)}")
    response = web.Response(body=body, content_type="application/javascript")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def proxy_http(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    if request.remote not in {"127.0.0.1", "::1"}:
        return plain_response(403, "Forbidden")
    if request.raw_path.startswith(("http://", "https://")):
        return plain_response(400, "Bad Request")
    try:
        async with client_session() as client:
            async with client.get(
                f"{runtime.config.upstream_origin}{request.raw_path}",
                headers=upstream_headers(runtime),
                allow_redirects=False,
                auto_decompress=False,
            ) as upstream:
                body = await read_bounded(upstream)
                response = web.Response(body=body, status=upstream.status)
                for name in FORWARDED_RESPONSE_HEADERS:
                    value = upstream.headers.get(name)
                    if value:
                        response.headers[name] = value
                response.headers["X-Content-Type-Options"] = "nosniff"
                if is_authorized_vnc_entry(request, runtime):
                    set_session_cookie(response, runtime)
                    log("session_created_from_vnc")
                return response
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
        log(f"http_proxy_failed path={request.raw_path} error={type(exc).__name__}: {exc}")
        return plain_response(502, "Bad Gateway")


async def pump_local_to_upstream(
    local: web.WebSocketResponse,
    upstream: aiohttp.ClientWebSocketResponse,
) -> None:
    byte_count = 0
    try:
        async for message in local:
            if message.type is aiohttp.WSMsgType.BINARY:
                if len(message.data) > MAX_BODY_BYTES:
                    log(f"ws_local_frame_rejected type={message.type} size={len(message.data)}")
                    return
                byte_count += len(message.data)
                await upstream.send_bytes(message.data)
                continue
            if message.type is aiohttp.WSMsgType.TEXT:
                byte_count += len(message.data)
                await upstream.send_str(message.data)
                continue
            if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}:
                return
            if message.type is aiohttp.WSMsgType.ERROR:
                log(f"ws_local_frame_error error={local.exception()}")
                return
            if len(message.data) > MAX_BODY_BYTES:
                log(f"ws_local_frame_rejected type={message.type} size={len(message.data) if message.data else 0}")
                return
            log(f"ws_local_frame_rejected type={message.type} size={len(message.data) if message.data else 0}")
            return
    finally:
        log(f"ws_local_to_upstream_done bytes={byte_count}")


async def pump_upstream_to_local(
    upstream: aiohttp.ClientWebSocketResponse,
    local: web.WebSocketResponse,
) -> None:
    byte_count = 0
    try:
        async for message in upstream:
            if message.type is aiohttp.WSMsgType.BINARY:
                if len(message.data) > MAX_BODY_BYTES:
                    log(f"ws_upstream_frame_rejected type={message.type} size={len(message.data)}")
                    return
                byte_count += len(message.data)
                await local.send_bytes(message.data)
                continue
            if message.type is aiohttp.WSMsgType.TEXT:
                byte_count += len(message.data)
                await local.send_str(message.data)
                continue
            if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}:
                return
            if message.type is aiohttp.WSMsgType.ERROR:
                log(f"ws_upstream_frame_error error={upstream.exception()}")
                return
            if len(message.data) > MAX_BODY_BYTES:
                log(f"ws_upstream_frame_rejected type={message.type} size={len(message.data) if message.data else 0}")
                return
            log(f"ws_upstream_frame_rejected type={message.type} size={len(message.data) if message.data else 0}")
            return
    finally:
        log(f"ws_upstream_to_local_done bytes={byte_count}")


async def relay_websocket(request: web.Request) -> web.StreamResponse:
    runtime = request.app[RUNTIME_KEY]
    if request.remote not in {"127.0.0.1", "::1"}:
        return plain_response(403, "Forbidden")

    runtime.viewer_active = True
    client: aiohttp.ClientSession | None = None
    upstream: aiohttp.ClientWebSocketResponse | None = None
    local: web.WebSocketResponse | None = None
    try:
        protocols = tuple(
            item.strip()
            for item in request.headers.get("Sec-WebSocket-Protocol", "binary").split(",")
            if item.strip()
        )
        local = web.WebSocketResponse(
            protocols=protocols,
            max_msg_size=0,
        )
        await local.prepare(request)
        log(f"ws_local_connected protocol={local.ws_protocol or '-'}")

        target = f"wss://{runtime.config.host}{request.rel_url}"
        client = client_session()
        log(f"ws_connect_upstream target={target}")
        upstream = await client.ws_connect(
            target,
            protocols=protocols,
            headers=upstream_headers(runtime),
            max_msg_size=0,
        )
        log(f"ws_upstream_connected protocol={upstream.protocol or '-'}")
        pumps = {
            asyncio.create_task(pump_local_to_upstream(local, upstream)),
            asyncio.create_task(pump_upstream_to_local(upstream, local)),
        }
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*done, *pending, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        log(f"ws_relay_done errors={len(errors)}")
        return local
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log(f"ws_relay_error error={type(exc).__name__}: {exc}")
        if local is None:
            return plain_response(502, "Bad Gateway")
        return local
    finally:
        if local is not None:
            await local.close()
        if upstream is not None:
            await upstream.close()
        if client is not None:
            await client.close()
        runtime.viewer_active = False


def build_app(runtime: Runtime) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app[RUNTIME_KEY] = runtime
    app.router.add_get("/", serve_html)
    app.router.add_get("/visual_index.html", serve_html)
    app.router.add_get("/embedded.html", serve_embedded_html)
    app.router.add_post("/api/session", exchange_session)
    app.router.add_get("/novnc/{path:.*}", proxy_javascript)
    app.router.add_get("/websockify", relay_websocket)
    app.router.add_get("/{path:.*}", proxy_http)
    return app


async def upstream_preflight(runtime: Runtime) -> None:
    async with client_session() as client:
        async with client.get(
            f"{runtime.config.upstream_origin}/core/rfb.js",
            headers=upstream_headers(runtime),
            allow_redirects=False,
        ) as response:
            body = await read_bounded(response)
            if response.status != 200 or b"export default" not in body or b"RFB" not in body:
                raise RuntimeError(
                    f"upstream noVNC preflight failed: status={response.status} bytes={len(body)}"
                )
        websocket = await client.ws_connect(
            f"wss://{runtime.config.host}/websockify",
            protocols=("binary",),
            headers=upstream_headers(runtime),
            heartbeat=20,
            max_msg_size=MAX_BODY_BYTES,
        )
        try:
            message = await websocket.receive(timeout=10)
            if message.type is not aiohttp.WSMsgType.BINARY or not message.data.startswith(b"RFB "):
                raise RuntimeError(f"upstream VNC preflight failed: message_type={message.type}")
            log(f"upstream_preflight_ok protocol={websocket.protocol or '-'} greeting={message.data[:11]!r}")
        finally:
            await websocket.close()


async def local_preflight(runtime: Runtime, port: int) -> None:
    """Exercise the same session, asset, and relay path used by the browser."""

    origin = f"http://127.0.0.1:{port}"
    health_ticket = runtime.ticket
    jar = aiohttp.CookieJar(unsafe=True)
    try:
        async with client_session(cookie_jar=jar) as client:
            async with client.get(f"{origin}{local_vnc_path(runtime)}") as response:
                if response.status != 200:
                    raise RuntimeError(f"local HTML preflight failed: status={response.status}")
                await response.read()
            async with client.post(
                f"{origin}/api/session",
                json={"ticket": health_ticket},
                headers={"Origin": origin},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"local session preflight failed: status={response.status}")
                await response.read()
            async with client.get(
                f"{origin}/novnc/core/rfb.js",
                headers={"Origin": origin},
            ) as response:
                body = await response.read()
                content_type = response.headers.get("Content-Type", "")
                if response.status != 200 or "javascript" not in content_type:
                    raise RuntimeError(
                        f"local asset preflight failed: status={response.status} content_type={content_type!r}"
                    )
                if b"export default" not in body or b"RFB" not in body:
                    raise RuntimeError("local asset preflight failed: unexpected rfb.js")
            websocket = await client.ws_connect(
                f"{origin.replace('http://', 'ws://', 1)}/websockify",
                protocols=("binary",),
                origin=origin,
                heartbeat=20,
                max_msg_size=MAX_BODY_BYTES,
            )
            try:
                message = await websocket.receive(timeout=10)
                if message.type is not aiohttp.WSMsgType.BINARY or not message.data.startswith(b"RFB "):
                    raise RuntimeError(f"local relay preflight failed: message_type={message.type}")
            finally:
                await websocket.close()

        deadline = time.monotonic() + 5
        while runtime.viewer_active and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if runtime.viewer_active:
            raise RuntimeError("local relay preflight did not close")
        log("local_preflight_ok")
    finally:
        runtime.session_id = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="sandbox 6080 host, without scheme")
    parser.add_argument("--password", required=True, help="VNC password")
    parser.add_argument("--task-id", default="unknown")
    parser.add_argument("--port", type=int, default=0, help="local listen port; 0 picks a free port")
    parser.add_argument("--index-file", type=Path, default=None, help="serve this visual index at /")
    parser.add_argument(
        "--envd-access-token",
        default=os.environ.get("RB_VIEWER_ENVD_ACCESS_TOKEN") or None,
    )
    parser.add_argument(
        "--traffic-access-token",
        default=os.environ.get("RB_VIEWER_TRAFFIC_ACCESS_TOKEN") or None,
    )
    parser.add_argument("--startup-check", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be between 0 and 65535")
    return args


def main() -> int:
    args = parse_args()
    config = ViewerConfig.create(
        host=args.host,
        password=args.password,
        task_id=args.task_id,
        envd_access_token=args.envd_access_token,
        traffic_access_token=args.traffic_access_token,
        index_path=args.index_file,
    )
    runtime = Runtime(config=config)
    runner = web.AppRunner(build_app(runtime))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", args.port)
        loop.run_until_complete(site.start())
        server = site._server
        if server is None or not server.sockets:
            raise RuntimeError("local viewer failed to listen")
        port = int(server.sockets[0].getsockname()[1])
        runtime.local_origin = f"http://127.0.0.1:{port}"
        loop.run_until_complete(upstream_preflight(runtime))
        if args.startup_check:
            loop.run_until_complete(local_preflight(runtime, port))
        visual_entry = f"{runtime.local_origin}/" if config.index_path is not None else f"{runtime.local_origin}{local_vnc_path(runtime)}"
        print(f"visual_local_server={visual_entry}", flush=True)
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
