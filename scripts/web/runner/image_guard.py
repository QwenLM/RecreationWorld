#!/usr/bin/env python3
"""Claude Code hook for RecreationBench Web's text-only evaluation mode.

Enabled only when ALLOW_IMAGE_READ is false (wired by runner/run_agent.py into
~/.claude/settings.json or the workspace .claude/settings.local.json). It makes
the agent unable to feed *rendered pixels* into the model's native multimodal
context, while leaving text tools (browser_snapshot / browser_evaluate / reading
HTML/DOM) and the act of *taking* a screenshot untouched.

Two events, dispatched on hook_event_name (read from stdin JSON):

  PreToolUse  + tool_name == "Read" + file_path is an image (by extension OR
              magic bytes, so renamed images are caught)
        -> permissionDecision "deny" with a reason the model sees. This blocks
           ONLY that one Read call and returns the message as feedback; the
           rollout continues (Claude Code does not terminate on a hook deny).

  PostToolUse + tool_name matches browser_take_screenshot
        -> additionalContext note next to the tool result, explaining that the
           screenshot was saved but its pixels are not viewable, and how to
           analyse the image via text tools instead.

Fail-open: any parse/IO error exits 0 (allow), so a hook bug can never abort a
rollout. Pixel blocking is additionally backstopped by the MCP server's
`--image-responses omit` flag, so fail-open here does not reopen the screenshot
inline-image path.
"""
import json
import sys

IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".avif", ".ico", ".heic", ".heif",
)

_DENY_READ_REASON = (
    "Text-only evaluation: reading image pixels via the model's native "
    "multimodal ability is disabled, so this image was NOT loaded. You can still "
    "analyse image information through text tools instead: use browser_evaluate to "
    "read element sizes, colors, computed styles and image source URLs; use "
    "browser_snapshot for the accessibility tree (including alt text and image "
    "URLs); or read the HTML/DOM source directly. Continue the task with these "
    "text tools and do not retry reading image files."
)

_SCREENSHOT_NOTE = (
    "The screenshot was saved to disk, but this is a text-only evaluation: the "
    "model's native vision is unavailable and reading the image file back is "
    "blocked. Image information is still obtainable through text tools, for "
    "example browser_evaluate (element sizes, colors, computed styles, image "
    "source URLs), browser_snapshot (accessibility tree with alt text), and the "
    "HTML/DOM source."
)


def _is_image(path: str) -> bool:
    """True if `path` is a raster image, by extension or by magic bytes."""
    if not path:
        return False
    if path.lower().endswith(IMAGE_EXTS):
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False  # can't read -> let normal flow handle it (likely errors anyway)
    if not head:
        return False
    if head.startswith(b"\x89PNG"):
        return True
    if head.startswith(b"\xff\xd8\xff"):          # JPEG
        return True
    if head[:4] in (b"GIF8",):                     # GIF87a / GIF89a
        return True
    if head.startswith(b"BM"):                     # BMP
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:4] in (b"II*\x00", b"MM\x00*"):       # TIFF (LE / BE)
        return True
    if head[:4] == b"\x00\x00\x01\x00":            # ICO
        return True
    if head[4:8] == b"ftyp":                        # AVIF / HEIC (ISO-BMFF)
        return True
    return False


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open: never break a rollout on malformed input

    event = data.get("hook_event_name") or data.get("hookEventName") or ""
    tool = data.get("tool_name") or data.get("toolName") or ""
    tool_input = data.get("tool_input") or data.get("toolInput") or {}

    if event == "PreToolUse" and tool == "Read":
        fp = tool_input.get("file_path") or tool_input.get("filePath") or ""
        if _is_image(fp):
            _emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _DENY_READ_REASON,
                }
            })
        return 0

    if event == "PostToolUse" and "browser_take_screenshot" in tool:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": _SCREENSHOT_NOTE,
            }
        })
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
