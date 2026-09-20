const screen = document.querySelector("#screen");
const video = document.querySelector("#video");
const screenWrap = document.querySelector("#screenWrap");
const screenMessage = document.querySelector("#screenMessage");
const gesture = document.querySelector("#gesture");
const viewer = document.querySelector(".viewer");
const connectionText = document.querySelector("#connectionText");
const appSelect = document.querySelector("#appSelect");
let frameUrl = null;
let frameBusy = false;
let frameEtag = null;
let pointerStart = null;
let deviceWidth = 0;
let deviceHeight = 0;
let currentPackage = "";
let videoReady = false;
let videoFrames = [];
let videoRetry = null;
let streamAbort = null;
let mediaUrl = null;

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  return response;
}

async function postInput(payload) {
  await api("/api/input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!videoReady) setTimeout(loadFrame, 100);
  setTimeout(loadStatus, 200);
}

async function loadStatus() {
  try {
    const status = await (await api("/api/status")).json();
    viewer.classList.toggle("offline", !status.connected);
    connectionText.textContent = status.connected ? "Live Android" : "Device offline";
    document.querySelector("#deviceName").textContent = status.model || "Android emulator";
    document.querySelector("#serial").textContent = status.serial || "—";
    document.querySelector("#androidVersion").textContent = status.apiLevel ? `API ${status.apiLevel}` : "—";
    document.querySelector("#viewport").textContent = status.width ? `${status.width} × ${status.height}` : "—";
    deviceWidth = status.width || deviceWidth;
    deviceHeight = status.height || deviceHeight;
    if (!videoReady) {
      const streamStatus = status.streamFps ? `PNG fallback · ${status.streamFps.toFixed(1)} FPS` : "Starting video…";
      document.querySelector("#stream").textContent = status.streamError || streamStatus;
    }
    currentPackage = status.package || "";
    document.querySelector("#package").textContent = currentPackage || "—";
    if (!status.connected) screenMessage.textContent = status.message || "Waiting for Android…";
  } catch (error) {
    viewer.classList.add("offline");
    connectionText.textContent = "Viewer unavailable";
    screenMessage.textContent = error.message;
  }
}

function formatScore(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value !== "number") return String(value);
  return value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

function formatResultStatus(result) {
  const value = String(result.evalStatus || result.stage || "running").toLowerCase();
  const labels = {
    recreation_eval: "Evaluated",
    resolved: "Evaluated",
    not_evaluated: "Not evaluated",
    recreation: "Recreation",
    eval: "Evaluating",
    running: "Running",
    failed: "Failed",
  };
  return labels[value] || value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

async function loadResult() {
  try {
    const result = await (await api("/api/result")).json();
    const taskId = result.taskId || "Android app";
    const runName = result.runName || "-";
    const statusText = formatResultStatus(result);
    const statusPill = document.querySelector("#resultStatus");
    const failed = /fail|error|timeout/.test(String(result.evalStatus || result.stage || ""));

    document.querySelector("#taskTitle").textContent = taskId;
    document.querySelector("#taskSubtitle").textContent = `android / ${runName}`;
    document.querySelector("#taskId").textContent = result.taskId || "-";
    document.querySelector("#runName").textContent = runName;
    document.querySelector("#taskScore").textContent = formatScore(result.taskScore);
    document.querySelector("#passed").textContent = result.passed === null || result.passed === undefined
      ? "-"
      : String(Boolean(result.passed));
    document.querySelector("#resultStatusText").textContent = statusText;
    statusPill.classList.toggle("failed", failed);

    const metricsLink = document.querySelector("#metricsLink");
    if (result.metricsAvailable) {
      metricsLink.href = "/metrics.json";
      metricsLink.target = "_blank";
      metricsLink.rel = "noreferrer";
      metricsLink.classList.remove("disabled");
      metricsLink.removeAttribute("aria-disabled");
    } else {
      metricsLink.removeAttribute("href");
      metricsLink.classList.add("disabled");
      metricsLink.setAttribute("aria-disabled", "true");
    }
  } catch (_) {
    document.querySelector("#resultStatusText").textContent = "Result unavailable";
  }
}

async function loadPackages() {
  try {
    const { packages } = await (await api("/api/packages")).json();
    const previousPackage = appSelect.value;
    appSelect.replaceChildren();
    if (!packages.length) {
      appSelect.add(new Option("No third-party apps installed", ""));
      return;
    }
    for (const packageName of packages) appSelect.add(new Option(packageName, packageName));
    if (packages.includes(currentPackage)) appSelect.value = currentPackage;
    else if (packages.includes(previousPackage)) appSelect.value = previousPackage;
  } catch (error) {
    appSelect.replaceChildren(new Option(error.message, ""));
  }
}

async function loadFrame() {
  if (videoReady || frameBusy || document.hidden) return;
  frameBusy = true;
  try {
    const headers = frameEtag ? { "If-None-Match": frameEtag } : {};
    const response = await fetch("/api/frame.png", { cache: "no-store", headers });
    if (response.status === 304) return;
    if (!response.ok) throw new Error(`Frame request failed (${response.status})`);
    frameEtag = response.headers.get("ETag");
    const blob = await response.blob();
    const nextUrl = URL.createObjectURL(blob);
    screen.onload = () => {
      if (frameUrl) URL.revokeObjectURL(frameUrl);
      frameUrl = nextUrl;
      screen.classList.add("ready");
      screenMessage.hidden = true;
    };
    screen.src = nextUrl;
  } catch (error) {
    screenMessage.hidden = false;
    screenMessage.textContent = error.message;
  } finally {
    frameBusy = false;
  }
}

function devicePoint(event) {
  const rect = screenWrap.getBoundingClientRect();
  const width = deviceWidth || screen.naturalWidth;
  const height = deviceHeight || screen.naturalHeight;
  const imageRatio = width / height;
  const boxRatio = rect.width / rect.height;
  const renderedWidth = imageRatio > boxRatio ? rect.width : rect.height * imageRatio;
  const renderedHeight = imageRatio > boxRatio ? rect.width / imageRatio : rect.height;
  const offsetX = (rect.width - renderedWidth) / 2;
  const offsetY = (rect.height - renderedHeight) / 2;
  return {
    x: Math.max(0, Math.min(width, (event.clientX - rect.left - offsetX) * width / renderedWidth)),
    y: Math.max(0, Math.min(height, (event.clientY - rect.top - offsetY) * height / renderedHeight)),
  };
}

screenWrap.addEventListener("pointerdown", (event) => {
  if (!deviceWidth && !screen.naturalWidth) return;
  screenWrap.setPointerCapture(event.pointerId);
  pointerStart = { ...devicePoint(event), clientX: event.clientX, clientY: event.clientY, at: performance.now() };
  gesture.style.left = `${event.clientX - screenWrap.getBoundingClientRect().left}px`;
  gesture.style.top = `${event.clientY - screenWrap.getBoundingClientRect().top}px`;
  gesture.classList.add("visible");
});

screenWrap.addEventListener("pointermove", (event) => {
  if (!pointerStart) return;
  gesture.style.left = `${event.clientX - screenWrap.getBoundingClientRect().left}px`;
  gesture.style.top = `${event.clientY - screenWrap.getBoundingClientRect().top}px`;
});

screenWrap.addEventListener("pointerup", async (event) => {
  if (!pointerStart) return;
  const start = pointerStart;
  pointerStart = null;
  gesture.classList.remove("visible");
  const end = devicePoint(event);
  const distance = Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY);
  try {
    if (distance < 12) {
      await postInput({ action: "tap", x: end.x, y: end.y });
    } else {
      await postInput({
        action: "swipe", x1: start.x, y1: start.y, x2: end.x, y2: end.y,
        duration: Math.max(100, performance.now() - start.at),
      });
    }
  } catch (error) { screenMessage.textContent = error.message; }
});

document.querySelectorAll("[data-key]").forEach((button) => {
  button.addEventListener("click", () => postInput({ action: "key", key: button.dataset.key }));
});

document.querySelector("#launch").addEventListener("click", async () => {
  if (appSelect.value) await postInput({ action: "launch", package: appSelect.value });
});

document.querySelector("#textForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#textInput");
  const status = document.querySelector("#inputStatus");
  if (!input.value) return;
  try {
    await postInput({ action: "text", text: input.value });
    status.textContent = "Text sent to Android.";
    input.value = "";
  } catch (error) { status.textContent = error.message; }
});

document.querySelector("#refresh").addEventListener("click", async () => {
  await Promise.all([loadStatus(), loadResult()]);
  await loadPackages();
  startVideo();
  loadFrame();
});

document.querySelector("#fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await document.documentElement.requestFullscreen();
    }
  } catch (_) {}
});

document.addEventListener("fullscreenchange", () => {
  document.querySelector("#fullscreen").textContent = document.fullscreenElement ? "Exit viewer" : "Open viewer";
});

function updateVideoFps(now) {
  videoFrames.push(now);
  const cutoff = now - 2000;
  while (videoFrames.length && videoFrames[0] < cutoff) videoFrames.shift();
  if (videoFrames.length > 1) {
    const elapsed = videoFrames.at(-1) - videoFrames[0];
    const fps = elapsed > 0 ? (videoFrames.length - 1) * 1000 / elapsed : 0;
    document.querySelector("#stream").textContent = `H.264 · ${fps.toFixed(1)} FPS`;
  }
  video.requestVideoFrameCallback(updateVideoFps);
}

function once(target, eventName) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      target.removeEventListener(eventName, success);
      target.removeEventListener("error", failure);
    };
    const success = (event) => { cleanup(); resolve(event); };
    const failure = () => { cleanup(); reject(new Error(`Media ${eventName} failed`)); };
    target.addEventListener(eventName, success, { once: true });
    target.addEventListener("error", failure, { once: true });
  });
}

async function appendBuffer(sourceBuffer, bytes) {
  sourceBuffer.appendBuffer(bytes);
  await once(sourceBuffer, "updateend");
  if (sourceBuffer.buffered.length) {
    const last = sourceBuffer.buffered.length - 1;
    const liveEdge = sourceBuffer.buffered.end(last);
    const lag = liveEdge - video.currentTime;
    if (video.currentTime === 0 || lag > 0.2) {
      video.currentTime = Math.max(sourceBuffer.buffered.start(last), liveEdge - 0.04);
    }
    if (video.paused) video.play().catch(() => {});
  }
  if (sourceBuffer.buffered.length && video.currentTime > 30) {
    sourceBuffer.remove(0, video.currentTime - 10);
    await once(sourceBuffer, "updateend");
  }
}

async function startVideo() {
  clearTimeout(videoRetry);
  if (streamAbort) streamAbort.abort();
  if (mediaUrl) URL.revokeObjectURL(mediaUrl);
  videoReady = false;
  videoFrames = [];
  video.classList.remove("ready");
  const codec = 'video/mp4; codecs="avc1.42C02A"';
  if (!("MediaSource" in window) || !MediaSource.isTypeSupported(codec)) {
    loadFrame();
    return;
  }

  const controller = new AbortController();
  streamAbort = controller;
  const mediaSource = new MediaSource();
  mediaUrl = URL.createObjectURL(mediaSource);
  video.src = mediaUrl;
  try {
    await once(mediaSource, "sourceopen");
    const sourceBuffer = mediaSource.addSourceBuffer(codec);
    const response = await api(`/api/stream.mp4?t=${Date.now()}`, { signal: controller.signal });
    if (!response.body) throw new Error("Streaming response is unavailable");
    const reader = response.body.getReader();
    video.play().catch(() => {});
    while (!controller.signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      await appendBuffer(sourceBuffer, value);
    }
    if (mediaSource.readyState === "open") mediaSource.endOfStream();
  } catch (error) {
    if (error.name === "AbortError" || controller !== streamAbort) return;
    videoReady = false;
    video.classList.remove("ready");
    screen.classList.add("ready");
    screenMessage.hidden = true;
    loadFrame();
    videoRetry = setTimeout(startVideo, 3000);
  }
}

video.addEventListener("playing", () => {
  videoReady = true;
  video.classList.add("ready");
  screen.classList.remove("ready");
  screenMessage.hidden = true;
  document.querySelector("#stream").textContent = "H.264 · live";
});

video.addEventListener("error", () => {
  if (streamAbort) streamAbort.abort();
  videoReady = false;
  video.classList.remove("ready");
  screen.classList.add("ready");
  screenMessage.hidden = true;
  loadFrame();
  videoRetry = setTimeout(startVideo, 3000);
});

if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
  video.requestVideoFrameCallback(updateVideoFps);
}

async function bootstrap() {
  await Promise.all([loadStatus(), loadResult()]);
  await loadPackages();
  loadFrame();
  startVideo();
}

bootstrap();
setInterval(loadFrame, 120);
setInterval(loadStatus, 5000);
setInterval(loadResult, 5000);
setInterval(loadPackages, 15000);
