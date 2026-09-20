"""Small RFB 3.8 probe used to verify a local RecreationBench viewer."""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
from urllib.parse import parse_qs, urldefrag, urlsplit

import aiohttp
from Crypto.Cipher import DES


def reverse_bits(value: int) -> int:
    value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
    value = ((value & 0xCC) >> 2) | ((value & 0x33) << 2)
    return ((value & 0xAA) >> 1) | ((value & 0x55) << 1)


def vnc_response(password: str, challenge: bytes) -> bytes:
    password_bytes = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(reverse_bits(value) for value in password_bytes)
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)


class BinaryReader:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.buffer = bytearray()

    async def read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            message = await self.websocket.receive(timeout=10)
            if message.type is not aiohttp.WSMsgType.BINARY:
                raise RuntimeError(f"unexpected WebSocket message: {message.type}")
            self.buffer.extend(message.data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


async def probe(viewer_url: str) -> dict[str, object]:
    base_url, ticket = urldefrag(viewer_url)
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("viewer URL must be a loopback URL")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    password_values = parse_qs(parsed.query).get("password", [])
    password = password_values[0] if password_values else None
    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as client:
        session_reused = False
        if password is None:
            async with client.post(
                f"{origin}/api/session",
                json={"ticket": ticket},
                headers={"Origin": origin},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"session exchange failed: status={response.status}")
                password = (await response.json())["password"]

            async with client.post(
                f"{origin}/api/session",
                json={"ticket": ""},
                headers={"Origin": origin},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"session reuse failed: status={response.status}")
                if (await response.json())["password"] != password:
                    raise RuntimeError("session reuse returned a different VNC password")
                session_reused = True

        websocket = await client.ws_connect(
            f"ws://{parsed.netloc}/websockify",
            protocols=("binary",),
            origin=origin,
            max_msg_size=4 * 1024 * 1024,
        )
        try:
            reader = BinaryReader(websocket)
            protocol = await reader.read(12)
            if not protocol.startswith(b"RFB 003."):
                raise RuntimeError(f"unexpected RFB protocol: {protocol!r}")
            await websocket.send_bytes(b"RFB 003.008\n")

            security_count = (await reader.read(1))[0]
            if security_count == 0:
                reason_length = struct.unpack(">I", await reader.read(4))[0]
                reason = (await reader.read(reason_length)).decode(errors="replace")
                raise RuntimeError(f"server rejected security negotiation: {reason}")
            security_types = list(await reader.read(security_count))
            if 2 in security_types:
                await websocket.send_bytes(b"\x02")
                challenge = await reader.read(16)
                await websocket.send_bytes(vnc_response(password, challenge))
            elif 1 in security_types:
                await websocket.send_bytes(b"\x01")
            else:
                raise RuntimeError(f"unsupported RFB security types: {security_types}")

            security_result = struct.unpack(">I", await reader.read(4))[0]
            if security_result != 0:
                reason_length = struct.unpack(">I", await reader.read(4))[0]
                reason = (await reader.read(reason_length)).decode(errors="replace")
                raise RuntimeError(f"VNC authentication failed: {reason}")

            await websocket.send_bytes(b"\x01")
            width, height = struct.unpack(">HH", await reader.read(4))
            await reader.read(16)
            name_length = struct.unpack(">I", await reader.read(4))[0]
            if name_length > 1024 * 1024:
                raise RuntimeError("desktop name is too large")
            name = (await reader.read(name_length)).decode(errors="replace")
            return {
                "rfb_protocol": protocol.decode().strip(),
                "security_types": security_types,
                "authenticated": True,
                "session_reused": session_reused,
                "width": width,
                "height": height,
                "desktop_name": name,
            }
        finally:
            await websocket.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("viewer_url")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.viewer_url)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
