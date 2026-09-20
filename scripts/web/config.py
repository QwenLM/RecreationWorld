"""RecreationBench Web evaluation configuration."""

import os
import sys
from pathlib import Path

# Screenshot capture already performs a bounded five-second document.fonts.ready
# wait. Playwright adds another internal fonts.ready wait inside page.screenshot;
# on some frozen sites (notably ClimateWatch) that second wait can wedge Chromium
# and grow a renderer to several GiB even though document.fonts.status is loaded.
# Skip only Playwright's duplicate wait; our explicit bounded wait remains active.
os.environ["PW_TEST_SCREENSHOT_NO_FONTS_READY"] = "1"

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from common import vlm_judge as _vlm_judge  # noqa: E402
from core import mcp_settings as _mcp  # noqa: E402

TEMPLATE_DIR = PROJECT_ROOT / "template"

# Crawling limits
MAX_CRAWL_PAGES = 100
MAX_CRAWL_DEPTH = 4

# Screenshot resolutions (simplified from v1's 7 to 2)
RESOLUTIONS = [
    {"width": 1920, "height": 1080, "name": "desktop"},
    {"width": 375, "height": 812, "name": "mobile"},
]

RESOLUTION_WEIGHTS = {
    "desktop": 0.60,
    "mobile": 0.40,
}

# Time limit for agent runs (seconds)
TIME_LIMIT = 7200  # 120 minutes

# Evaluation test dimension weights
TEST_WEIGHTS = {
    "functional": 0.50,
    "visual": 0.50,
    "structural": 0.0,
    "quality": 0.0,
}

# Screenshot wait strategy
SCREENSHOT_WAIT = {
    "wait_until": "networkidle",
    "extra_wait_ms": 3000,
    "scroll_for_lazy_load": True,
}

# Server configuration
SERVER_PORT_RANGE = (8100, 9100)

# Page load settings
PAGE_LOAD_TIMEOUT = 60000
PAGE_WAIT_MS = 2000

# Deterministic random seed (reuse from v1 for consistency)
BENCH_RANDOM_SEED = 42

# Semantic HTML tags for structural analysis
SEMANTIC_TAGS = {"header", "nav", "main", "footer", "section", "article", "aside"}

# Region selectors for screenshot extraction.
# NOTE: "hero" was intentionally removed (P2-1 / bug #5). It is a non-semantic, naming-
# dependent design pattern, so GT and an honest rebuild routinely select structurally
# different elements (or the rebuild has none) → ~11% catastrophic near-zero region
# scores that unfairly punished honest work while rewarding verbatim copies. The
# remaining regions are stable semantic landmarks present in both GT and honest rebuilds.
REGION_SELECTORS = {
    "header": ['header', '[role="banner"]', 'nav:first-of-type'],
    "main-content": ['main', '[role="main"]', '#main-content', '#content'],
    "footer": ['footer', '[role="contentinfo"]'],
}

# Skip tags for DOM extraction
SKIP_TAGS = {"script", "style", "noscript", "svg", "link", "meta", "template", "iframe"}

# Browser MCP server configurations
BROWSER_MCP_CONFIGS = {
    "playwright": {
        "type": "stdio",
        # deployment adapter installs this executable from the exact package pin. Invoking
        # it directly prevents npx from resolving another version or reaching npm
        # after the agent is isolated.
        "command": _mcp.PLAYWRIGHT_BINARY,
        # --no-sandbox: the agent runs as root in the container, where Chromium's
        #   sandbox cannot start. The concrete browser binary is selected at
        #   runtime by setup_browser_mcp(), which appends
        #   `--executable-path <bundled chromium>` — @playwright/mcp otherwise
        #   defaults to the `chrome` channel / chrome-for-testing, which is not
        #   installed in the image (only `playwright install chromium` is).
        # --allowed-origins: the browser may only reach loopback — the reference
        #   site and the agent's own preview server, both on arbitrary localhost
        #   ports (hence the port wildcard). Without it the default is "allow all"
        #   and browser_navigate is an open egress path to, say, the original
        #   site's GitHub repo. Upstream is explicit that this is NOT a security
        #   boundary and does not follow redirects, so treat it as defence in
        #   depth: the real control is runner/net_isolate, whose namespace the MCP
        #   inherits (it is spawned by the agent), leaving the browser no route out
        #   at all. This flag is what still holds when isolation is explicitly
        #   disabled with MOCKWEB_NET_ISOLATION=0 for local development.
        "args": [
            "--headless", "--no-sandbox",
            "--allowed-origins", "http://localhost:*;http://127.0.0.1:*",
        ],
    },
}

DEFAULT_BROWSER_MCP = "playwright"


def image_read_allowed() -> bool:
    """Whether the agent may feed rendered image PIXELS into the model.

    Controlled by the ALLOW_IMAGE_READ env var (task param `allow_image_read`,
    auto-injected uppercased by the platform). Default True = status quo. When
    False, runner.run_agent installs the image_guard hook + the MCP
    `--image-responses omit` flag so the agent can still take screenshots but
    cannot ingest pixels (text-only evaluation).
    """
    import os
    # Fail-safe to status quo (allow) for unset/empty; only an explicit
    # false-y value turns the ban on.
    return os.environ.get("ALLOW_IMAGE_READ", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def vlm_judge_max_concurrency() -> int | None:
    """Read the deployment platform/profile override even when the image entrypoint drops its CLI flag.

    The unified pipeline exposes ``vlm_judge_max_concurrency`` as an deployment platform parameter,
    which deployment adapter exports as ``VLM_JUDGE_MAX_CONCURRENCY``.  Older Web images do
    not translate that environment variable into ``--vlm-max-concurrency`` when
    they invoke this RB-owned runner.  Reading it here keeps the release control in
    RB and makes a pinned checkout independent of the image wrapper version.
    """
    raw = os.environ.get("VLM_JUDGE_MAX_CONCURRENCY", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "VLM_JUDGE_MAX_CONCURRENCY must be a positive integer, "
            f"got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError(
            "VLM_JUDGE_MAX_CONCURRENCY must be a positive integer, "
            f"got {raw!r}"
        )
    return value


# Default eval config
DEFAULT_EVAL_CONFIG = {
    "scoring_mode": "tests_only",
    # NOTE: test_weights here is INFORMATIONAL only. score_aggregator always uses
    # config.TEST_WEIGHTS (the authoritative source); per-task eval_config
    # test_weights are not consumed by scoring.
    "test_weights": TEST_WEIGHTS,
    "vlm_judge": {
        # DEFAULT OFF: the VLM judge is disabled unless explicitly turned on at
        # runtime (runner.run_agent --vlm-judge / the task's USE_VLM_JUDGE param).
        # When off, the visual dimension is scored by SSIM/LPIPS/Layout-IoU. This
        # avoids a mis-provisioned judge (for example, a missing VLM_API_KEY) silently
        # zeroing the visual dimension. Runtime overrides win over this AND over any
        # baked evaluation/eval_config.json (see evaluator.evaluate_task).
        "enabled": False,
        "mode": "assertion",
        "backend": "openai-compatible",
        "model": _vlm_judge.DEFAULT_MODEL,
        # When enabled, the VLM judge provides the visual dimension score
        # (replacing SSIM/LPIPS) in the four-dimension weighted sum.
        # assertion mode: per-page atomic visual assertions (vlm_assertions.json),
        # judged binary against the agent screenshot.
        "max_concurrency": 8,
        "base_url": _vlm_judge.DEFAULT_BASE_URL,
        # api_key: None -> resolve via common.vlm_judge.KEY_ENVS.
        "api_key": None,
    },
    "screenshot_resolutions": RESOLUTIONS,
    "screenshot_wait": SCREENSHOT_WAIT,
}
