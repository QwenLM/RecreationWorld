"""GT (Ground Truth) screenshot and DOM snapshot generator.

Generates desktop and mobile screenshots plus DOM snapshots for all
eligible pages in a site, used as reference for visual and structural evaluation.
"""

import asyncio
import base64
import json
import logging
from io import BytesIO
from pathlib import Path

from core import runtime_assets
from config import RESOLUTIONS as VIEWPORTS

logger = logging.getLogger(__name__)

# DOM serialization script (reused from v1 extraction/dom_extractor.py)
SERIALIZE_DOM_SCRIPT = runtime_assets.load_text("web/runtime_assets/serialize_dom.js")

# Fixed random seed for deterministic JS behavior
BENCH_RANDOM_SEED = 42

# Bounding-box layout extraction (for Layout-IoU visual sub-score). Returns
# semantic elements with ABSOLUTE page coords (computed at any scroll position),
# plus viewport width and full page height so the IoU can normalise coordinates.
EXTRACT_BBOX_SCRIPT = runtime_assets.load_text("web/runtime_assets/extract_bbox.js")
WAIT_FOR_FONTS_SCRIPT = runtime_assets.load_text("web/runtime_assets/wait_for_fonts.js")
FREEZE_ANIMATIONS_SCRIPT = runtime_assets.load_text(
    "web/runtime_assets/freeze_animations.js"
)
EMPTY_MOUNT_SCRIPT = runtime_assets.load_text("web/runtime_assets/empty_mount.js")
SCREENCAST_REPAINT_SCRIPT = runtime_assets.load_text(
    "web/runtime_assets/screencast_repaint.js"
)
SCREENCAST_SCROLL_SCRIPT = runtime_assets.load_text(
    "web/runtime_assets/screencast_scroll.js"
)

# Viewports: single source of truth shared with agent capture. The evaluator
# captures agent screenshots via config.RESOLUTIONS, so deriving VIEWPORTS from
# the same constant guarantees GT and agent iterate identical viewports (no
# silent desync of mobile-emulation / screenshot dir names).
async def _stabilize_page(page, settle_ms: int = 300):
    """Scroll through page to trigger lazy-loading, then wait for height to stabilize."""
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(settle_ms)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(settle_ms)

    prev_height = await page.evaluate("() => document.body.scrollHeight")
    for _ in range(5):
        await page.wait_for_timeout(settle_ms)
        cur_height = await page.evaluate("() => document.body.scrollHeight")
        if cur_height == prev_height:
            break
        prev_height = cur_height


async def _force_fonts_ready(page):
    """Wait for fonts to load with a timeout."""
    try:
        await page.evaluate(WAIT_FOR_FONTS_SCRIPT)
    except Exception:
        pass


async def _disable_animations(page):
    """Inject CSS to freeze animations for deterministic screenshots."""
    await page.add_init_script(FREEZE_ANIMATIONS_SCRIPT)


async def _seed_random(page, seed: int = BENCH_RANDOM_SEED):
    """Override Math.random() with a seeded PRNG."""
    script = runtime_assets.render_text(
        "web/runtime_assets/seeded_random.js.template", {"__SEED__": seed}
    )
    await page.add_init_script(script)


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


async def _block_external_requests(context):
    """Abort any non-local request so a capture can only ever load THIS served tree.

    Defense-in-depth for GT determinism/self-containment: the cleanroom pages are
    self-contained (localhost only), so for a correctly-served page this is a no-op. But if
    a page ever referenced an external host — or the served tree is momentarily the wrong one
    — this guarantees no foreign pixels bleed into the capture. localhost/127.0.0.1 (any
    port), plus data:/blob:/about: URIs, are always allowed.
    """
    async def _route(route):
        u = route.request.url
        if u.startswith((
            "http://localhost", "https://localhost",
            "http://127.0.0.1", "https://127.0.0.1",
            "data:", "blob:", "about:",
        )):
            try:
                await route.continue_()
            except Exception:
                pass
        else:
            try:
                await route.abort()
            except Exception:
                pass
    try:
        await context.route("**/*", _route)
    except Exception:
        pass


def _context_kwargs(vp: dict) -> dict:
    """Browser-context kwargs for a viewport.

    Mobile uses real device semantics (is_mobile/has_touch/UA) so responsive layouts
    engage at 375px. DSF=1 is deliberate: full-page height is dynamic after lazy loading,
    so DSF=3 can allocate multi-gigabyte rasters before an oversize guard can observe the
    final height. Image comparison normalises both dimensions across DSFs.
    """
    if vp.get("name") == "mobile":
        return {
            "viewport": {"width": vp["width"], "height": vp["height"]},
            "device_scale_factor": 1,
            "is_mobile": True,
            "has_touch": True,
            "user_agent": MOBILE_USER_AGENT,
            "reduced_motion": "reduce",
        }
    return {
        "viewport": {"width": vp["width"], "height": vp["height"]},
        "reduced_motion": "reduce",
    }


async def _wait_for_content(page, viewport_height: int, max_ms: int = 6000):
    """Bounded wait for real content to render.

    If the page is still <= one viewport tall it likely hasn't rendered yet (the
    networkidle->domcontentloaded fallback can fire early on archived pages whose
    external requests never settle), which produced blank single-viewport GT
    captures (audit H4). Tall pages return immediately; blank/slow pages wait up
    to max_ms; genuinely short real pages just pay the bounded wait.
    """
    waited = 0
    while waited < max_ms:
        h = await page.evaluate("() => (document.body ? document.body.scrollHeight : 0)")
        if h > viewport_height + 50:
            return
        await page.wait_for_timeout(400)
        waited += 400


async def _reload_if_empty_mount(page, url: str, viewport_height: int):
    """SPA mount-failure recovery (agent capture robustness, 2026-07-07).

    A client-rendered app can transiently fail to hydrate on the first load (slow async
    mount, one-off runtime hiccup), leaving an empty ``#root``/``#app``/``#__next`` shell that
    would otherwise be screenshotted blank and score ~0 on visual+functional. This does ONE
    reload with a longer settle — but ONLY when the page rendered essentially nothing, so a
    normally-rendered page (the overwhelming majority) is never touched and GT/agent capture
    stays symmetric on all non-empty pages. Static GT pages (real DOM, no empty SPA root)
    never trigger it. It cannot fix a hard, deterministic runtime crash — only flaky mounts.
    """
    try:
        empty = await page.evaluate(EMPTY_MOUNT_SCRIPT)
    except Exception:
        return
    if not empty:
        return
    logger.warning("Empty mount detected for %s; one reload retry", url)
    try:
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
        await _wait_for_content(page, viewport_height)
    except Exception as e:
        logger.warning("Empty-mount reload failed for %s: %s", url, str(e)[:120])


async def _capture_regions(page, out_dir: str):
    """Capture per-region element screenshots (header/hero/main-content/footer)
    into <out_dir>/<region>.png via config.REGION_SELECTORS. Missing regions are
    skipped. Used for region-level visual scoring (#8)."""
    from config import REGION_SELECTORS
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name, selector in REGION_SELECTORS.items():
        # config.REGION_SELECTORS values may be a LIST of CSS selectors → join into
        # a comma (union) selector; the current schema stores selector lists.
        sel = ", ".join(selector) if isinstance(selector, (list, tuple)) else selector
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await loc.screenshot(path=str(Path(out_dir) / f"{name}.png"), timeout=15000)
        except Exception as e:
            logger.debug("region %s skipped: %s", name, str(e)[:80])


# Oversized-page guard: some archived pages render to an enormous height (blog/document
# listings, long legal filings). Mobile uses DSF=1 above so dynamic lazy-loaded pages cannot
# allocate a 3x-tall raster before their final document height is observable.
# OVERSIZE_DOC_HEIGHT: measured document height (CSS px) above which we DETERMINISTICALLY switch
#   to the bounded capture (rather than attempting full_page and hoping it does not time out —
#   that would make the capture non-deterministic, breaking the determinism gate). Set well above
#   every healthy page's height (desktop tops out ~43k px) so ONLY genuinely-uncapturable pages
#   are affected — zero change for any other page's GT.
# CAPPED_GT_HEIGHT: the bounded top-region height captured for such pages.
OVERSIZE_DOC_HEIGHT = 50000  # CSS px — deterministic trigger
CAPPED_GT_HEIGHT = 16000     # CSS px — bounded capture height
TILE_CAPTURE_HEIGHT = 4000   # CSS px — bounds each Chromium paint operation
SCREENCAST_FRAME_TIMEOUT = 10
SCREENCAST_FRAME_ATTEMPTS = 3
SCREENCAST_TILES_PER_SESSION = 12
SCREENCAST_CLEANUP_TIMEOUT = 5
CONTEXT_CLEANUP_TIMEOUT = 10
MOBILE_TILES_PER_CONTEXT = 4
MOBILE_CDP_TIMEOUT = 15


class ScreencastFrameTimeout(RuntimeError):
    """Chromium did not publish a viewport frame after bounded retries.

    This deliberately does not inherit from ``TimeoutError``: the caller also
    has an outer per-view deadline, and conflating the two made a missing CDP
    frame look like the entire 420-second capture budget had elapsed.
    """


async def _doc_height(page) -> int:
    try:
        return int(await page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight||0,"
            " (document.body&&document.body.scrollHeight)||0,"
            " document.documentElement.offsetHeight||0)"))
    except Exception:
        return 0


async def _capped_capture(browser, url: str, output_path: str, vp: dict,
                          layout_path: str = None) -> str:
    """Capture an extreme-height document at DSF=1 and bound the result.

    No CSS height hack is used because max-height/overflow changes can collapse layouts.
    The screenshot is cropped after rendering, without altering the page layout.
    """
    kw = dict(_context_kwargs(vp))
    kw["device_scale_factor"] = 1  # the key lever: up to 9x fewer pixels than mobile DSF=3
    ctx = await browser.new_context(**kw)
    await _block_external_requests(ctx)
    try:
        import os as _os
        from PIL import Image as _Image
        _Image.MAX_IMAGE_PIXELS = None  # tall clones legitimately exceed the decompression-bomb cap
        p = await ctx.new_page()
        await _disable_animations(p)
        await _seed_random(p)
        try:
            await p.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            await p.goto(url, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(2000)
        await _wait_for_content(p, vp["height"])
        await _reload_if_empty_mount(p, url, vp["height"])
        try:
            await _stabilize_page(p, settle_ms=200)
        except Exception:
            pass
        try:
            await _force_fonts_ready(p)
        except Exception:
            pass
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # Playwright's `clip` without full_page is clamped to the viewport, so capture the
        # full DSF=1 page first and crop the resulting pixels without altering page layout.
        tmp = str(output_path) + ".full.png"
        await p.screenshot(path=tmp, full_page=True, timeout=90000)
        try:
            im = _Image.open(tmp)
            cap_px = CAPPED_GT_HEIGHT  # DSF=1 -> 1 CSS px == 1 device px
            if im.height > cap_px:
                im.crop((0, 0, im.width, cap_px)).save(output_path)
            else:
                im.save(output_path)
            im.close()
        finally:
            try:
                _os.remove(tmp)
            except Exception:
                pass
        if layout_path:
            try:
                await p.evaluate("window.scrollTo(0, 0)")
                layout = await p.evaluate(EXTRACT_BBOX_SCRIPT)
                Path(layout_path).parent.mkdir(parents=True, exist_ok=True)
                Path(layout_path).write_text(json.dumps(layout, ensure_ascii=False))
            except Exception as e:
                logger.warning("capped bbox layout extract failed for %s: %s", url, str(e)[:100])
        return "capped"
    finally:
        await ctx.close()


class _ViewportScreencast:
    """Read the visible Chromium surface without invoking screenshot capture.

    ``Page.captureScreenshot`` can allocate a compositor surface as tall as the
    document even when ``captureBeyondViewport`` is false.  ClimateWatch's mobile
    renderer consequently grew to 6--8 GiB and crashed.  CDP screencast frames are
    produced by the already-visible viewport and stay bounded by its dimensions.
    """

    def __init__(self, page, width: int, height: int):
        self.page = page
        self.width = width
        self.height = height
        self.session = None
        self.frames = asyncio.Queue(maxsize=1)
        self._ack_tasks = set()
        self._handler = None

    async def start(self):
        self.session = await self.page.context.new_cdp_session(self.page)

        def on_frame(params):
            while not self.frames.empty():
                try:
                    self.frames.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self.frames.put_nowait(params)
            except asyncio.QueueFull:
                pass
            task = asyncio.create_task(
                self._ack(params.get("sessionId"))
            )
            self._ack_tasks.add(task)
            task.add_done_callback(self._ack_tasks.discard)

        self._handler = on_frame
        self.session.on("Page.screencastFrame", on_frame)
        await self.session.send("Page.enable", {})
        await self.session.send(
            "Page.startScreencast",
            {
                "format": "png",
                "maxWidth": self.width,
                "maxHeight": self.height,
                "everyNthFrame": 1,
            },
        )
        return self

    async def _ack(self, session_id):
        if self.session is None or session_id is None:
            return
        try:
            await self.session.send(
                "Page.screencastFrameAck", {"sessionId": session_id}
            )
        except Exception:
            pass

    def discard_pending(self):
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _force_repaint(self):
        """Bring the page forward and force a paint even when it cannot scroll."""
        if self.session is not None:
            try:
                await self.session.send("Page.bringToFront")
            except Exception:
                pass
        await self.page.evaluate(SCREENCAST_REPAINT_SCRIPT)

    async def read_latest(self) -> bytes:
        params = None
        for attempt in range(1, SCREENCAST_FRAME_ATTEMPTS + 1):
            try:
                params = await asyncio.wait_for(
                    self.frames.get(), timeout=SCREENCAST_FRAME_TIMEOUT
                )
                break
            except asyncio.TimeoutError as exc:
                if attempt == SCREENCAST_FRAME_ATTEMPTS:
                    if self.session is not None:
                        try:
                            logger.warning(
                                "Chromium screencast stopped producing frames; "
                                "falling back to a bounded viewport capture"
                            )
                            screenshot = await asyncio.wait_for(
                                self.session.send(
                                    "Page.captureScreenshot",
                                    {
                                        "format": "png",
                                        # Capture from the visible browser view.  The
                                        # surface-backed path can allocate a raster as
                                        # tall as the whole document even when
                                        # captureBeyondViewport is false.
                                        "fromSurface": False,
                                        "captureBeyondViewport": False,
                                    },
                                ),
                                timeout=SCREENCAST_FRAME_TIMEOUT,
                            )
                            data = screenshot.get("data")
                            if data:
                                return base64.b64decode(data)
                        except Exception as fallback_error:
                            logger.warning(
                                "Bounded viewport capture also failed: %s",
                                fallback_error,
                            )
                    raise ScreencastFrameTimeout(
                        "Chromium produced no screencast frame after "
                        f"{SCREENCAST_FRAME_ATTEMPTS} attempts"
                    ) from exc
                logger.warning(
                    "Chromium produced no screencast frame in %ss; "
                    "forcing repaint (attempt %d/%d)",
                    SCREENCAST_FRAME_TIMEOUT,
                    attempt + 1,
                    SCREENCAST_FRAME_ATTEMPTS,
                )
                try:
                    await self._force_repaint()
                except Exception as repaint_error:
                    logger.debug("Screencast repaint failed: %s", repaint_error)
        # A scroll may generate more than one paint. Give the final paint a short
        # opportunity to replace the intermediate frame, then use the newest one.
        await asyncio.sleep(0.05)
        while not self.frames.empty():
            params = self.frames.get_nowait()
        return base64.b64decode(params["data"])

    async def close(self):
        if self.session is None:
            return
        try:
            await asyncio.wait_for(
                self.session.send("Page.stopScreencast"),
                timeout=SCREENCAST_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out stopping Chromium screencast")
        except Exception:
            pass
        if self._ack_tasks:
            done, pending = await asyncio.wait(
                tuple(self._ack_tasks), timeout=SCREENCAST_CLEANUP_TIMEOUT
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.exception()
                except (asyncio.CancelledError, Exception):
                    pass
        if self._handler is not None and hasattr(self.session, "remove_listener"):
            self.session.remove_listener("Page.screencastFrame", self._handler)
        try:
            await asyncio.wait_for(
                self.session.detach(), timeout=SCREENCAST_CLEANUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out detaching Chromium screencast session")
        except Exception:
            pass
        finally:
            self.session = None


async def _scroll_for_screencast(page, wanted_top: int) -> dict:
    """Move the real page scroller and force a final viewport paint."""
    return await page.evaluate(SCREENCAST_SCROLL_SCRIPT, wanted_top)


def _paste_viewport_tile(
    canvas,
    tile,
    *,
    width: int,
    target_height: int,
    wanted_top: int,
    actual_top: int,
    state: dict,
    output_scale: float | None,
):
    """Paste one viewport using CSS coordinates and return updated stitch state.

    Mobile pages without a viewport meta tag have a layout viewport wider and
    taller than the screenshot bitmap.  Scroll offsets are expressed in that
    CSS coordinate space, so treating them as bitmap pixels loses the bottom of
    the page.  Normalize through the layout viewport width before cropping.
    """
    from PIL import Image as _Image

    css_viewport_width = max(1, int(state.get("viewportWidth") or width))
    css_viewport_height = max(
        1, int(state.get("viewportHeight") or tile.height)
    )
    current_scale = width / css_viewport_width
    if output_scale is None:
        output_scale = current_scale
        canvas = _Image.new(
            "RGB", (width, max(1, round(target_height * output_scale)))
        )
    elif abs(current_scale - output_scale) > max(0.01, output_scale * 0.02):
        raise RuntimeError(
            "viewport scale changed while capturing "
            f"({current_scale:.4f} != {output_scale:.4f})"
        )

    expected_tile_height = max(1, round(css_viewport_height * output_scale))
    if tile.size != (width, expected_tile_height):
        tile = tile.resize(
            (width, expected_tile_height), _Image.Resampling.LANCZOS
        )

    source_top_css = max(0, wanted_top - actual_top)
    available_css = max(0, css_viewport_height - source_top_css)
    take_css = min(target_height - wanted_top, available_css)
    if take_css <= 0:
        raise RuntimeError(
            f"viewport capture made no progress at y={wanted_top} "
            f"(scrollY={actual_top}, viewport={tile.height}, "
            f"page={target_height})"
        )

    source_top = round(source_top_css * output_scale)
    source_bottom = min(
        tile.height, round((source_top_css + take_css) * output_scale)
    )
    dest_top = round(wanted_top * output_scale)
    dest_bottom = round((wanted_top + take_css) * output_scale)
    if source_bottom <= source_top or dest_bottom <= dest_top:
        raise RuntimeError(
            f"viewport capture rounded to an empty tile at y={wanted_top}"
        )
    strip = tile.crop((0, source_top, width, source_bottom))
    if strip.height != dest_bottom - dest_top:
        strip = strip.resize(
            (width, dest_bottom - dest_top), _Image.Resampling.LANCZOS
        )
    canvas.paste(strip, (0, dest_top))
    return canvas, output_scale, wanted_top + take_css


async def _tiled_full_page_capture(page, output_path: str, width: int,
                                   height: int) -> str:
    """Capture a complete page through viewport-sized strips and stitch at DSF=1.

    Chromium still allocates a surface from the document origin when Playwright is
    given an absolute ``clip`` below the viewport.  On very tall pages that both
    defeats the memory bound and eventually makes the final clip fall outside the
    compositor surface.  Scrolling first and capturing the viewport keeps every
    paint bounded.  The final scroll is normally clamped to ``height - viewport``;
    crop the required suffix from that viewport instead of requesting an invalid
    absolute clip.
    """
    from PIL import Image as _Image

    _Image.MAX_IMAGE_PIXELS = None
    canvas = None
    output_scale = None
    viewport = page.viewport_size or {}
    viewport_height = max(1, int(viewport.get("height") or 1))
    captured_until = 0
    target_height = height

    screencast = await _ViewportScreencast(
        page, width, viewport_height
    ).start()
    tiles_in_session = 0
    try:
        while captured_until < target_height:
            if (
                screencast is not None
                and tiles_in_session >= SCREENCAST_TILES_PER_SESSION
            ):
                await screencast.close()
                screencast = await _ViewportScreencast(
                    page, width, viewport_height
                ).start()
                tiles_in_session = 0
            wanted_top = captured_until
            # A fresh screencast's initial frame is the only frame a short,
            # non-scrolling page may ever publish.  Preserve it for the first
            # tile; later tiles must discard the frame from the prior position.
            if captured_until > 0:
                screencast.discard_pending()
            state = await _scroll_for_screencast(page, wanted_top)
            actual_top = max(0, int(state.get("scrollY") or 0))
            current_height = max(viewport_height, int(state.get("height") or 0))
            target_height = min(target_height, current_height)
            if wanted_top >= target_height:
                break

            encoded = await screencast.read_latest()
            tiles_in_session += 1
            with _Image.open(BytesIO(encoded)) as tile:
                tile = tile.convert("RGB")
                canvas, output_scale, captured_until = _paste_viewport_tile(
                    canvas,
                    tile,
                    width=width,
                    target_height=target_height,
                    wanted_top=wanted_top,
                    actual_top=actual_top,
                    state=state,
                    output_scale=output_scale,
                )

        if canvas is None or output_scale is None:
            raise RuntimeError("viewport capture produced no tiles")
        final_height = max(1, round(target_height * output_scale))
        if canvas.height != final_height:
            cropped = canvas.crop((0, 0, width, final_height))
            canvas.close()
            canvas = cropped
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # Persist the completed capture before asking an overloaded renderer to
        # tear its CDP session down.  Cleanup is best-effort and must not turn an
        # already-written screenshot into a missing view.
        canvas.save(output_path)
    finally:
        try:
            if screencast is not None:
                await screencast.close()
        finally:
            if canvas is not None:
                canvas.close()
    return "tiled_full_page"


async def _open_mobile_tile_page(browser, url: str, vp: dict):
    """Open one short-lived renderer for a bounded batch of mobile tiles."""
    context = await browser.new_context(**_context_kwargs(vp))
    await _block_external_requests(context)
    try:
        page = await context.new_page()
        await _disable_animations(page)
        await _seed_random(page)
        # These contexts are intentionally short-lived and repeat the same
        # self-contained frozen page. Waiting for networkidle in every batch can
        # spend 30 seconds on harmless background timers; the bounded content and
        # font waits below are the actual readiness gates needed for capture.
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        await _wait_for_content(page, vp["height"])
        await _reload_if_empty_mount(page, url, vp["height"])
        await _force_fonts_ready(page)
        session = await asyncio.wait_for(
            context.new_cdp_session(page), timeout=MOBILE_CDP_TIMEOUT
        )
        await asyncio.wait_for(
            session.send("Page.enable", {}), timeout=MOBILE_CDP_TIMEOUT
        )
        return context, page, session
    except BaseException:
        try:
            await asyncio.wait_for(
                context.close(), timeout=CONTEXT_CLEANUP_TIMEOUT
            )
        except Exception:
            pass
        raise


async def _close_mobile_tile_page(context, session):
    """Best-effort bounded cleanup for a mobile tile renderer."""
    if session is not None:
        try:
            await asyncio.wait_for(
                session.detach(), timeout=SCREENCAST_CLEANUP_TIMEOUT
            )
        except Exception:
            pass
    if context is not None:
        try:
            await asyncio.wait_for(
                context.close(), timeout=CONTEXT_CLEANUP_TIMEOUT
            )
        except Exception:
            pass


async def _restartable_tiled_full_page_capture(
    browser, url: str, output_path: str, vp: dict, height: int
) -> str:
    """Capture mobile pages without allowing one renderer to grow unbounded.

    Chromium's viewport screenshot path leaks compositor memory as a long page is
    scrolled.  Rotating only the CDP screencast session does not release that
    renderer; ClimateWatch reaches several GiB and loses the target after a few
    tiles.  Recreate the browser context after a small tile batch so the renderer
    (and its compositor surfaces) is actually reclaimed.
    """
    from PIL import Image as _Image

    _Image.MAX_IMAGE_PIXELS = None
    width = int(vp["width"])
    viewport_height = max(1, int(vp["height"]))
    target_height = max(viewport_height, int(height))
    canvas = None
    output_scale = None
    captured_until = 0
    failures = 0
    context = page = session = None

    try:
        while captured_until < target_height:
            context = page = session = None
            try:
                context, page, session = await _open_mobile_tile_page(
                    browser, url, vp
                )
                tiles = 0
                while (
                    captured_until < target_height
                    and tiles < MOBILE_TILES_PER_CONTEXT
                ):
                    wanted_top = captured_until
                    state = await _scroll_for_screencast(page, wanted_top)
                    actual_top = max(0, int(state.get("scrollY") or 0))
                    current_height = max(
                        viewport_height, int(state.get("height") or 0)
                    )
                    if current_height < target_height:
                        raise RuntimeError(
                            "mobile page height changed while capturing "
                            f"({current_height} < {target_height})"
                        )

                    result = await asyncio.wait_for(
                        session.send(
                            "Page.captureScreenshot",
                            {
                                "format": "png",
                                "fromSurface": False,
                                "captureBeyondViewport": False,
                            },
                        ),
                        timeout=MOBILE_CDP_TIMEOUT,
                    )
                    encoded = result.get("data")
                    if not encoded:
                        raise RuntimeError("Chromium returned an empty viewport frame")

                    with _Image.open(BytesIO(base64.b64decode(encoded))) as tile:
                        tile = tile.convert("RGB")
                        canvas, output_scale, captured_until = (
                            _paste_viewport_tile(
                                canvas,
                                tile,
                                width=width,
                                target_height=target_height,
                                wanted_top=wanted_top,
                                actual_top=actual_top,
                                state=state,
                                output_scale=output_scale,
                            )
                        )
                    tiles += 1
                failures = 0
            except Exception as exc:
                failures += 1
                if failures >= 2:
                    raise
                logger.warning(
                    "Mobile tile renderer failed at y=%d; retrying in a fresh "
                    "context: %s",
                    captured_until,
                    str(exc)[:120],
                )
            finally:
                await _close_mobile_tile_page(context, session)

        if canvas is None:
            raise RuntimeError("mobile viewport capture produced no tiles")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path)
        return "restartable_tiled_full_page"
    finally:
        if canvas is not None:
            canvas.close()


async def capture_screenshot(browser, url: str, output_path: str, vp: dict,
                             layout_path: str = None, region_dir: str = None) -> str:
    """Capture a full-page screenshot in a fresh, correctly-emulated context, and
    optionally the bbox-layout JSON (#7) and region screenshots (#8) from the SAME
    rendered page. Shared by GT build, incremental re-capture, and agent eval so
    all fixes apply everywhere. Returns capture mode
    ("full_page"/"tiled_full_page"/"viewport_only"/"capped").

    #2 fix: full-page screenshots get a longer timeout + one retry before the
    viewport-only fallback (the old single 45s attempt produced ~8% single-viewport
    captures on very tall pages).

    #3 fix: pages that reflow past OVERSIZE_DOC_HEIGHT (CSS px) are captured DETERMINISTICALLY
    via _capped_capture (fresh DSF=1 context + bounded top-region clip) instead of attempting a
    full_page screenshot that may time out non-deterministically.
    The threshold is set above every healthy page's height, so only genuinely-uncapturable pages
    are affected — no other page's GT changes. A capped capture is also the last-resort fallback
    if a sub-threshold page unexpectedly fails full_page + retry.
    """
    context = await browser.new_context(**_context_kwargs(vp))
    await _block_external_requests(context)
    try:
        page = await context.new_page()
        await _disable_animations(page)
        await _seed_random(page)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        await _wait_for_content(page, vp["height"])
        # SPA mount-failure recovery: one reload ONLY if the app rendered an empty shell.
        await _reload_if_empty_mount(page, url, vp["height"])

        try:
            await _stabilize_page(page, settle_ms=200)
        except Exception:
            pass
        try:
            await _force_fonts_ready(page)
        except Exception:
            pass

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Deterministic oversize guard for extreme CSS-height documents. Mobile already runs
        # at DSF=1, so lazy-load stabilization above cannot allocate a 3x-height raster.
        doc_h = max(vp["height"], await _doc_height(page))
        if vp.get("name") == "mobile":
            # Save layout from the already-stabilised page, then release that
            # renderer before the bounded, restartable viewport capture begins.
            if layout_path:
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                    layout = await page.evaluate(EXTRACT_BBOX_SCRIPT)
                    Path(layout_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(layout_path).write_text(
                        json.dumps(layout, ensure_ascii=False)
                    )
                except Exception as e:
                    logger.warning(
                        "bbox layout extract failed for %s: %s",
                        url,
                        str(e)[:100],
                    )
            await _close_mobile_tile_page(context, None)
            return await _restartable_tiled_full_page_capture(
                browser, url, output_path, vp, doc_h
            )
        elif doc_h > OVERSIZE_DOC_HEIGHT:
            logger.info("Oversized page (%d CSS px) -> capped capture: %s", doc_h, url)
            await context.close()
            return await _capped_capture(browser, url, output_path, vp, layout_path)
        else:
            mode = "full_page"
            try:
                await page.screenshot(path=output_path, full_page=True, timeout=90000)
            except Exception as e1:
                logger.warning("Full-page screenshot retry for %s: %s", url, str(e1)[:120])
                try:
                    await page.wait_for_timeout(1000)
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.screenshot(path=output_path, full_page=True, timeout=90000)
                except Exception as e2:
                    logger.warning("Oversized-page capped capture for %s: %s", url, str(e2)[:120])
                    try:
                        mode = await _capped_capture(browser, url, output_path, vp, layout_path)
                        return mode  # capped path wrote its own layout; regions are best-effort
                    except Exception as e3:
                        logger.warning("Capped capture also failed, viewport-only for %s: %s", url, str(e3)[:120])
                        await page.screenshot(path=output_path, full_page=False, timeout=15000)
                        mode = "viewport_only"

        # #7: bbox layout JSON (capture at scroll=0; coords are absolute regardless)
        if layout_path:
            try:
                await page.evaluate("window.scrollTo(0, 0)")
                layout = await page.evaluate(EXTRACT_BBOX_SCRIPT)
                Path(layout_path).parent.mkdir(parents=True, exist_ok=True)
                Path(layout_path).write_text(json.dumps(layout, ensure_ascii=False))
            except Exception as e:
                logger.warning("bbox layout extract failed for %s: %s", url, str(e)[:100])

        # #8: region screenshots
        if region_dir:
            try:
                await _capture_regions(page, region_dir)
            except Exception as e:
                logger.warning("region capture failed for %s: %s", url, str(e)[:100])

        return mode
    finally:
        try:
            # Chromium can finish writing a tall-page screenshot and then wedge
            # indefinitely while closing its renderer.  Keep teardown inside the
            # per-view deadline so the caller can move on to the next page.
            await asyncio.wait_for(
                context.close(), timeout=CONTEXT_CLEANUP_TIMEOUT
            )  # idempotent-safe: the oversize branch may have closed it already
        except asyncio.TimeoutError:
            logger.warning("Timed out closing Chromium screenshot context for %s", url)
        except Exception:
            pass


async def take_gt_screenshot(page, url: str, output_path: str, viewport: dict):
    """Take a GT screenshot with full wait strategy.

    Follows the proposal's wait strategy:
    1. Navigate with networkidle
    2. Wait for domcontentloaded
    3. Extra 3s wait for lazy-load/fonts/animations
    4. Scroll to bottom and back to trigger lazy-loading
    5. Full-page screenshot
    """
    await page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        logger.warning("networkidle timeout for %s, falling back to domcontentloaded", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(5000)

    await page.wait_for_timeout(3000)

    await _stabilize_page(page)
    await _force_fonts_ready(page)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(3):
        try:
            await page.screenshot(path=output_path, full_page=True, timeout=30000)
            return
        except Exception as e:
            if attempt == 2:
                raise
            logger.warning("Screenshot retry %d for %s: %s", attempt + 1, output_path, e)
            await asyncio.sleep(1.0 * (attempt + 1))


async def extract_dom_snapshot(page, url: str, wait_ms: int = 2000) -> dict:
    """Extract DOM snapshot from a page."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(wait_ms)
    dom = await page.evaluate(SERIALIZE_DOM_SCRIPT)
    dom["url"] = url
    return dom


async def generate_gt_for_page(
    browser,
    base_url: str,
    page_info: dict,
    gt_screenshots_dir: Path,
    gt_dom_dir: Path,
    gt_layout_dir: Path = None,
    gt_region_dir: Path = None,
    server=None,
):
    """Generate GT data for a single page (fresh, emulated context per viewport).

    Captures, per viewport: full-page screenshot + bbox-layout JSON (#7); region
    screenshots (#8) are captured once at the desktop viewport.

    ``server`` (optional SiteServer): when given, its identity is RE-ASSERTED before each
    viewport capture and once after the last one, so a mid-run port steal (a foreign station's
    server taking a freed port under concurrency) makes the capture fail LOUD instead of silently
    saving foreign pixels. Cross-station GT contamination guard (2026-07-20 residual).
    """
    page_id = page_info["id"]
    page_path = page_info["path"]
    url = base_url.rstrip("/") + page_path

    # Screenshots at both viewports (mobile uses real device emulation — audit H4)
    for vp in VIEWPORTS:
        # Re-verify the server we are about to navigate to still serves THIS station. The check
        # runs at an idle moment (the previous viewport's context is closed), so it does not race
        # the single-threaded handler. A definite foreign/dead server raises; a transient hiccup
        # is tolerated (see SiteServer.assert_serving).
        if server is not None:
            await server.assert_serving(f"before {page_id}/{vp['name']}")
        output_path = str(gt_screenshots_dir / page_id / f"{vp['name']}.png")
        layout_path = (str(gt_layout_dir / page_id / f"{vp['name']}.json")
                       if gt_layout_dir is not None else None)
        region_dir = (str(gt_region_dir / page_id)
                      if (gt_region_dir is not None and vp.get("name") == "desktop") else None)
        try:
            mode = await capture_screenshot(browser, url, output_path, vp,
                                            layout_path=layout_path, region_dir=region_dir)
            logger.debug("GT screenshot: %s/%s (%s)", page_id, vp["name"], mode)
        except Exception as e:
            logger.warning("Failed GT screenshot for %s/%s: %s", page_id, vp["name"], e)

    # Final identity assertion AFTER the (longer, higher-DSF) mobile capture — the widest window
    # in which a port steal could have contaminated below-the-fold / lazy-loaded content. If the
    # port flipped during the capture we abort this page loud rather than ship foreign pixels.
    if server is not None:
        await server.assert_serving(f"after {page_id}")

    # DOM snapshot (desktop context)
    try:
        ctx = await browser.new_context(**_context_kwargs(VIEWPORTS[0]))
        await _block_external_requests(ctx)
        try:
            p = await ctx.new_page()
            dom = await extract_dom_snapshot(p, url)
        finally:
            await ctx.close()
        dom_dir = gt_dom_dir / page_id
        dom_dir.mkdir(parents=True, exist_ok=True)
        with open(dom_dir / "dom_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(dom, f, indent=2, ensure_ascii=False)
        logger.debug("GT DOM: %s", page_id)
    except Exception as e:
        logger.warning("Failed GT DOM for %s: %s", page_id, e)


async def generate_gt_for_site(
    site_dir: Path,
    site_meta: dict,
    eval_dir: Path,
):
    """Generate GT screenshots and DOM snapshots for all eligible pages.

    Args:
        site_dir: Path to the site/ directory
        site_meta: Site metadata dict
        eval_dir: Path to the evaluation/ directory
    """
    from serving.site_server import SiteServer
    from playwright.async_api import async_playwright

    eligible_pages = [
        p for p in site_meta.get("pages", [])
        if p.get("oracle_quality") != "rejected"
    ]

    if not eligible_pages:
        logger.warning("No eligible pages for GT generation")
        return

    gt_screenshots_dir = eval_dir / "gt_screenshots"
    gt_dom_dir = eval_dir / "gt_dom"
    gt_layout_dir = eval_dir / "gt_layout"
    gt_region_dir = eval_dir / "gt_regions"
    for d in (gt_screenshots_dir, gt_dom_dir, gt_layout_dir, gt_region_dir):
        d.mkdir(parents=True, exist_ok=True)

    async with SiteServer(str(site_dir)) as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            for page_info in eligible_pages:
                try:
                    await generate_gt_for_page(
                        browser, server.url, page_info,
                        gt_screenshots_dir, gt_dom_dir,
                        gt_layout_dir, gt_region_dir,
                        server=server,
                    )
                except Exception as e:
                    logger.error("GT generation failed for %s: %s", page_info["id"], e)

            await browser.close()

    logger.info(
        "GT generated for %d pages in %s",
        len(eligible_pages), site_meta.get("domain", "?"),
    )
