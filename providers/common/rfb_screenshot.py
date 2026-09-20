"""Capture one framebuffer from a local RecreationBench noVNC relay."""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
from pathlib import Path
from urllib.parse import parse_qs, urldefrag, urlsplit

import aiohttp
from Crypto.Cipher import DES
from PIL import Image


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
            message = await self.websocket.receive(timeout=15)
            if message.type is not aiohttp.WSMsgType.BINARY:
                raise RuntimeError(f"unexpected WebSocket message: {message.type}")
            self.buffer.extend(message.data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


def decode_raw_pixels(pixel_format: dict[str, int], payload: bytes, width: int, height: int) -> bytes:
    bpp = pixel_format["bits_per_pixel"]
    if bpp % 8 != 0:
        raise RuntimeError(f"unsupported bits_per_pixel={bpp}")
    step = bpp // 8
    expected = width * height * step
    if len(payload) != expected:
        raise RuntimeError(f"unexpected raw rectangle bytes={len(payload)} expected={expected}")

    red_max = pixel_format["red_max"]
    green_max = pixel_format["green_max"]
    blue_max = pixel_format["blue_max"]
    red_shift = pixel_format["red_shift"]
    green_shift = pixel_format["green_shift"]
    blue_shift = pixel_format["blue_shift"]
    byteorder = "big" if pixel_format["big_endian"] else "little"

    rgb = bytearray(width * height * 3)
    out = 0
    for offset in range(0, len(payload), step):
        value = int.from_bytes(payload[offset : offset + step], byteorder=byteorder)
        red = ((value >> red_shift) & red_max) * 255 // red_max
        green = ((value >> green_shift) & green_max) * 255 // green_max
        blue = ((value >> blue_shift) & blue_max) * 255 // blue_max
        rgb[out : out + 3] = bytes((red, green, blue))
        out += 3
    return bytes(rgb)


async def authenticate(reader: BinaryReader, websocket: aiohttp.ClientWebSocketResponse, password: str) -> tuple[int, int, dict[str, int], str]:
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
    pixel_bytes = await reader.read(16)
    name_length = struct.unpack(">I", await reader.read(4))[0]
    name = (await reader.read(name_length)).decode(errors="replace")
    pixel_format = {
        "bits_per_pixel": pixel_bytes[0],
        "depth": pixel_bytes[1],
        "big_endian": pixel_bytes[2],
        "true_color": pixel_bytes[3],
        "red_max": struct.unpack(">H", pixel_bytes[4:6])[0],
        "green_max": struct.unpack(">H", pixel_bytes[6:8])[0],
        "blue_max": struct.unpack(">H", pixel_bytes[8:10])[0],
        "red_shift": pixel_bytes[10],
        "green_shift": pixel_bytes[11],
        "blue_shift": pixel_bytes[12],
    }
    if not pixel_format["true_color"]:
        raise RuntimeError("indexed-color framebuffer is unsupported")
    return width, height, pixel_format, name


async def capture(viewer_url: str, output: Path) -> dict[str, object]:
    base_url, ticket = urldefrag(viewer_url)
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("viewer URL must be a local HTTP URL")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    password_values = parse_qs(parsed.query).get("password", [])
    password = password_values[0] if password_values else None

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as client:
        if password is None:
            async with client.post(
                f"{origin}/api/session",
                json={"ticket": ticket},
                headers={"Origin": origin},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"session exchange failed: status={response.status}")
                password = (await response.json())["password"]

        websocket = await client.ws_connect(
            f"ws://{parsed.netloc}/websockify",
            protocols=("binary",),
            origin=origin,
            max_msg_size=0,
        )
        try:
            reader = BinaryReader(websocket)
            width, height, pixel_format, name = await authenticate(reader, websocket, password)
            await websocket.send_bytes(struct.pack(">BBHI", 2, 0, 1, 0))
            await websocket.send_bytes(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

            rgb = bytearray(width * height * 3)
            got_rectangle = False
            for _ in range(80):
                message_type = (await reader.read(1))[0]
                if message_type == 0:
                    await reader.read(1)
                    rectangle_count = struct.unpack(">H", await reader.read(2))[0]
                    for _rect_index in range(rectangle_count):
                        x, y, rect_width, rect_height, encoding = struct.unpack(">HHHHi", await reader.read(12))
                        if encoding != 0:
                            raise RuntimeError(f"unsupported framebuffer encoding={encoding}")
                        payload = await reader.read(rect_width * rect_height * (pixel_format["bits_per_pixel"] // 8))
                        rect_rgb = decode_raw_pixels(pixel_format, payload, rect_width, rect_height)
                        for row in range(rect_height):
                            src_start = row * rect_width * 3
                            dst_start = ((y + row) * width + x) * 3
                            rgb[dst_start : dst_start + rect_width * 3] = rect_rgb[src_start : src_start + rect_width * 3]
                        got_rectangle = True
                    if got_rectangle:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        Image.frombytes("RGB", (width, height), bytes(rgb)).save(output)
                        return {
                            "output": str(output),
                            "width": width,
                            "height": height,
                            "desktop_name": name,
                            "pixel_format": pixel_format,
                        }
                elif message_type == 2:
                    continue
                elif message_type == 3:
                    length = struct.unpack(">I", await reader.read(7)[3:7])[0]
                    await reader.read(length)
                else:
                    raise RuntimeError(f"unexpected server message type={message_type}")
        finally:
            await websocket.close()
    raise RuntimeError("no framebuffer update received")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("viewer_url")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(capture(args.viewer_url, args.output)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
