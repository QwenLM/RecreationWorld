#!/usr/bin/env python3
"""Print a compact, base64-free summary of a Claude trajectory."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main(path: str) -> None:
    turns = 0
    tool_uses: dict[str, int] = {}
    errors = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            turns += 1
            for content in event.get("message", {}).get("content", []):
                if content.get("type") == "tool_use":
                    name = content.get("name", "?")
                    tool_uses[name] = tool_uses.get(name, 0) + 1
            if event.get("error"):
                errors.append(event["error"])
        elif event_type == "result":
            usage = event.get("usage", {})
            cost = event.get("total_cost_usd", 0)
            print(f"  turns: {turns}")
            print(
                f"  tools: {dict(sorted(tool_uses.items(), key=lambda item: -item[1]))}"
            )
            print(f"  errors: {errors if errors else 'none'}")
            print(f"  cost: ${cost:.2f}")
            print(
                f"  tokens: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}"
            )
            result = re.sub(
                r"[A-Za-z0-9+/=]{200,}", "<base64>", str(event.get("result", ""))
            )
            print(f"  result: {result[:300]}")


if __name__ == "__main__":
    main(sys.argv[1])
