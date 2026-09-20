#!/usr/bin/env node
// Capture the page controlled by Playwright MCP through the same browser's CDP
// endpoint. This process never launches a second browser. In --stdio mode it
// keeps one CDP connection and one bounded viewport screencast alive for the
// complete agent run. Chromium emits at most one unacknowledged frame, so the
// helper acknowledges a frame only when the next capture is requested. This
// avoids both whole-document raster allocation and repeated start/stop calls.

const fs = require("fs");
const readline = require("readline");

function loadPlaywright() {
  // This helper lives in the commit-pinned RB tree, while Playwright is baked
  // into the Web image. Node resolves bare imports relative to this file, not
  // the current workspace, so include the two runtime dependency roots that
  // worker.sh already validates instead of relying on an ambient NODE_PATH.
  const roots = [
    process.env.MOCKWEB_NODE_MODULES,
    process.env.MOCKWEB_TEMPLATE_NODE_MODULES,
  ].filter(Boolean);
  for (const root of roots) {
    for (const moduleName of ["playwright", "@playwright/test"]) {
      try {
        return require(`${root}/${moduleName}`);
      } catch (error) {
        if (!error || error.code !== "MODULE_NOT_FOUND") throw error;
      }
    }
  }
  return require("playwright");
}

const { chromium } = loadPlaywright();

async function connect(endpoint) {
  return chromium.connectOverCDP(endpoint, { timeout: 5000 });
}

function delay(timeoutMs) {
  return new Promise((resolve) => setTimeout(resolve, timeoutMs));
}

async function activePage(browser, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  do {
    const pages = browser.contexts().flatMap((context) => context.pages())
      .filter((page) => !page.isClosed());
    if (pages.length) return pages[pages.length - 1];
    await delay(50);
  } while (Date.now() < deadline);
  throw new Error(`live MCP browser has no page after ${timeoutMs}ms`);
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`${label} timed out after ${timeoutMs}ms`)),
        timeoutMs,
      );
    }),
  ]).finally(() => clearTimeout(timer));
}

async function captureDirect(browser, output) {
  const page = await activePage(browser);
  const context = typeof page.context === "function" ? page.context() : null;
  // This is retained for the backwards-compatible one-shot CLI and as a
  // fallback for old Playwright builds without CDP sessions. The stdio path
  // below reads viewport-sized screencast frames instead.
  if (context && typeof context.newCDPSession === "function") {
    const session = await context.newCDPSession(page);
    try {
      const result = await withTimeout(
        session.send("Page.captureScreenshot", {
          format: "png",
          // A surface-backed capture can allocate a raster as tall as the
          // document even when captureBeyondViewport is false. ClimateWatch
          // was observed growing the renderer to several GiB on that path.
          fromSurface: false,
          captureBeyondViewport: false,
          optimizeForSpeed: true,
        }),
        8000,
        "Page.captureScreenshot",
      );
      fs.writeFileSync(output, Buffer.from(result.data, "base64"));
      return;
    } finally {
      await session.detach().catch(() => {});
    }
  }
  await page.screenshot({ path: output, timeout: 8000 });
}

class ViewportFrameStream {
  constructor() {
    this.page = null;
    this.session = null;
    this.latest = null;
    this.serial = 0;
    this.waiters = new Set();
    this.handler = null;
    this.streaming = false;
    this.dimensions = {};
    this.pendingFrameId = null;
  }

  async stop() {
    if (!this.session || !this.streaming) return;
    this.streaming = false;
    await this.ackPending().catch(() => {});
    await withTimeout(
      this.session.send("Page.stopScreencast"),
      500,
      "Page.stopScreencast",
    );
  }

  async close() {
    const session = this.session;
    const handler = this.handler;
    await this.stop().catch(() => {});
    this.page = null;
    this.session = null;
    this.handler = null;
    this.pendingFrameId = null;
    for (const resolve of this.waiters) resolve();
    this.waiters.clear();
    if (!session) return;
    if (handler && typeof session.removeListener === "function") {
      session.removeListener("Page.screencastFrame", handler);
    } else if (handler && typeof session.off === "function") {
      session.off("Page.screencastFrame", handler);
    }
    await withTimeout(
      session.detach().catch(() => {}),
      500,
      "CDP session detach",
    ).catch(() => {});
  }

  async ensure(page) {
    if (this.page === page && this.session) return;
    await this.close();
    const context = typeof page.context === "function" ? page.context() : null;
    if (!context || typeof context.newCDPSession !== "function") return;
    const session = await withTimeout(
      context.newCDPSession(page),
      3000,
      "new CDP screencast session",
    );
    this.page = page;
    this.session = session;
    this.latest = null;
    const handler = (frame) => {
      if (frame && frame.data) {
        this.latest = frame.data;
        this.pendingFrameId = frame.sessionId;
        this.serial += 1;
        for (const resolve of this.waiters) resolve();
        this.waiters.clear();
      }
    };
    this.handler = handler;
    session.on("Page.screencastFrame", handler);
    try {
      const viewport = typeof page.viewportSize === "function" ? page.viewportSize() : null;
      this.dimensions = viewport && viewport.width && viewport.height
        ? { maxWidth: viewport.width, maxHeight: viewport.height }
        : {};
      this.streaming = true;
      await withTimeout(
        session.send("Page.startScreencast", {
          format: "png",
          everyNthFrame: 1,
          ...this.dimensions,
        }),
        5000,
        "Page.startScreencast",
      );
      await this.waitForFrameAfter(0, 5000);
    } catch (error) {
      await this.close();
      throw error;
    }
  }

  async waitForFrameAfter(serial, timeoutMs) {
    if (this.latest && this.serial > serial) return true;
    return new Promise((resolve) => {
      let timer;
      const ready = () => {
        clearTimeout(timer);
        this.waiters.delete(ready);
        resolve(true);
      };
      this.waiters.add(ready);
      if (this.latest && this.serial > serial) return ready();
      timer = setTimeout(() => {
        this.waiters.delete(ready);
        resolve(false);
      }, timeoutMs);
    });
  }

  async ackPending() {
    if (!this.session || this.pendingFrameId === null) return;
    const sessionId = this.pendingFrameId;
    this.pendingFrameId = null;
    await withTimeout(
      this.session.send("Page.screencastFrameAck", { sessionId }),
      1000,
      "Page.screencastFrameAck",
    );
  }

  async capture(page, output) {
    await this.ensure(page);
    if (!this.session) {
      await page.screenshot({ path: output, timeout: 8000 });
      return;
    }
    // Release the previously held frame and briefly wait for Chromium to emit
    // a post-tool-use paint. Static pages need not repaint; in that case the
    // last frame is still the current viewport and is safe to reuse.
    const previousSerial = this.serial;
    await this.ackPending();
    await this.waitForFrameAfter(previousSerial, 250);
    if (!this.latest) throw new Error("Chromium produced no screencast frame");
    fs.writeFileSync(output, Buffer.from(this.latest, "base64"));
  }
}

async function oneShot(endpoint, output) {
  const browser = await connect(endpoint);
  await captureDirect(browser, output);
  // Do not call browser.close(): this is the browser MCP's live browser. Process
  // exit drops only this temporary CDP connection.
}

async function serve(endpoint) {
  let browser;
  const frames = new ViewportFrameStream();
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of input) {
    try {
      const request = JSON.parse(line);
      if (!request.output) throw new Error("capture request has no output path");
      if (!browser || (typeof browser.isConnected === "function" && !browser.isConnected())) {
        await frames.close();
        browser = await connect(endpoint);
      }
      const page = await activePage(browser);
      await frames.capture(page, request.output);
      process.stdout.write(`${JSON.stringify({ ok: true })}\n`);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({
        ok: false,
        error: String(error && error.stack ? error.stack : error).slice(0, 2000),
      })}\n`);
    }
  }
}

async function main() {
  const endpoint = process.argv[2];
  const output = process.argv[3];
  if (!endpoint || !output) {
    throw new Error("usage: capture_browser.js CDP OUTPUT|--stdio");
  }
  if (output === "--stdio") return serve(endpoint);
  return oneShot(endpoint, output);
}

main().then(
  // The CDP socket otherwise keeps Node alive until the monitor times out and
  // discards the completed PNG. Exit drops our connection without closing MCP's
  // browser (browser.close would terminate that shared browser).
  () => process.exit(0),
  (error) => {
    process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
    process.exit(1);
  },
);
