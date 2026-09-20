#!/usr/bin/env node
/** Deterministic Playwright adapter for the declarative Lathe demo scenario. */

import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { readFile, rename, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";
import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";

import { normalizedToolEvents } from "./evidence.mjs";
import { runScenario } from "./interpreter.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "out");
const VIDEO_TMP = "/tmp/capture-video";
rmSync(OUT_DIR, { recursive: true, force: true });
mkdirSync(OUT_DIR, { recursive: true });
mkdirSync(resolve(OUT_DIR, "screenshots"), { recursive: true });
mkdirSync(resolve(OUT_DIR, "diagnostics"), { recursive: true });
mkdirSync(VIDEO_TMP, { recursive: true });

try {
  for (const line of readFileSync(resolve(__dirname, "..", ".env"), "utf-8").split("\n")) {
    const match = line.match(/^([A-Za-z_]\w*)=(.*)$/);
    if (match && !(match[1] in process.env)) process.env[match[1]] = match[2];
  }
} catch {}

const SCENARIO = JSON.parse(readFileSync(resolve(__dirname, "scenario.json"), "utf-8"));
const OWUI_URL = (process.env.DEMO_OWUI_URL || "").replace(/\/+$/, "");
const MODEL = (process.env.DEMO_MODEL || process.env.OWUI_MODEL || "")
  .replace(/^(["'])(.*)\1$/, "$2");
let PASSKEY;
try { PASSKEY = JSON.parse(process.env.DEMO_PASSKEY || ""); } catch {}
if (!OWUI_URL || !MODEL || !PASSKEY?.rpId || !PASSKEY?.id || !PASSKEY?.privateKey) {
  const message = "Set DEMO_OWUI_URL, DEMO_PASSKEY, and DEMO_MODEL";
  const now = new Date().toISOString();
  await writeFile(resolve(OUT_DIR, "capture-report.json"), `${JSON.stringify({
    schema_version: 1,
    scenario: SCENARIO.id,
    scenario_version: SCENARIO.version,
    status: "rejected",
    failure_domain: "infrastructure",
    failed_step: "bootstrap",
    error: message,
    started_at: now,
    completed_at: now,
    steps: [],
  }, null, 2)}\n`);
  throw new Error(message);
}

const CHAT_URL = `${OWUI_URL}/?model=${encodeURIComponent(MODEL)}`;
const VIEWPORT = { width: 1280, height: 720 };
const ALLOWED_PROXY_DOMAINS = (
  process.env.DEMO_PROXY_DOMAINS
  || "adamsmith.systems,daytonaproxy01.net,proxy.app.daytona.io,proxy.daytona.work"
).split(",").map((item) => item.trim().toLowerCase()).filter(Boolean);

const TARGETS = {
  "owui.chat-input": ["#chat-input"],
  "owui.send": ["#send-message-button"],
  "owui.integration-menu": ["#integration-menu-button", "#tools-menu-button"],
  "editor.workbench": [".monaco-workbench"],
  "editor.terminal": [".terminal-wrapper", ".xterm"],
};

const started = Date.now();
function log(scope, message) {
  console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${scope}: ${message}`);
}

function target(name) {
  const selectors = TARGETS[name];
  if (!selectors) throw new Error(`Unknown logical target ${name}`);
  return selectors.join(",");
}

async function suppressIncidentalUi(page) {
  await page.evaluate(() => {
    if (document.getElementById("_capture_style")) return;
    const style = document.createElement("style");
    style.id = "_capture_style";
    style.textContent = "[data-tooltip]:before,[data-tooltip]:after,.tooltip,[role=tooltip],#model-selector-model-button{display:none!important;visibility:hidden!important}";
    document.head.appendChild(style);
    for (const button of document.querySelectorAll("button")) {
      if (button.textContent.trim() === "Okay, Let's Go!") button.click();
    }
  });
}

async function installTutorialCursor(page) {
  await page.evaluate(() => {
    if (document.getElementById("_demo_cursor")) return;
    const style = document.createElement("style");
    style.textContent = `
      #_demo_cursor {
        position: fixed; z-index: 2147483647; width: 22px; height: 22px;
        border-radius: 50%; pointer-events: none; opacity: 0;
        background: rgba(37, 99, 235, .72); border: 2px solid white;
        box-shadow: 0 0 0 2px rgba(37, 99, 235, .8), 0 4px 16px rgba(0, 0, 0, .3);
        transform: translate(-50%, -50%) scale(1);
        transition: left .55s cubic-bezier(.22,.8,.35,1), top .55s cubic-bezier(.22,.8,.35,1),
                    transform .16s ease, opacity .2s ease;
      }
      #_demo_cursor.clicking { transform: translate(-50%, -50%) scale(1.55); }
      #_demo_cursor_ring {
        position: fixed; z-index: 2147483646; pointer-events: none;
        border: 3px solid rgba(37, 99, 235, .82); border-radius: 10px;
        box-shadow: 0 0 18px rgba(37, 99, 235, .42);
        animation: _demo_cursor_pulse .65s ease-in-out infinite alternate;
      }
      @keyframes _demo_cursor_pulse { from { opacity: .55; } to { opacity: 1; } }
    `;
    document.head.appendChild(style);
    const cursor = document.createElement("div");
    cursor.id = "_demo_cursor";
    cursor.style.left = "-30px";
    cursor.style.top = "-30px";
    document.body.appendChild(cursor);
  });
}

async function moveTutorialCursor(page, locator, pauseMs = 350) {
  const box = await locator.boundingBox();
  if (!box) throw new Error("Tutorial cursor target has no visible bounding box");
  await page.evaluate(({ x, y }) => {
    const cursor = document.getElementById("_demo_cursor");
    cursor.style.opacity = "1";
    cursor.style.left = `${x}px`;
    cursor.style.top = `${y}px`;
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  await page.waitForTimeout(600 + pauseMs);
}

async function emphasizeTutorialTarget(page, locator, durationMs = 900) {
  const box = await locator.boundingBox();
  if (!box) return;
  await page.evaluate(({ box, duration }) => {
    document.getElementById("_demo_cursor_ring")?.remove();
    const ring = document.createElement("div");
    ring.id = "_demo_cursor_ring";
    Object.assign(ring.style, {
      left: `${box.x - 6}px`, top: `${box.y - 6}px`,
      width: `${box.width + 12}px`, height: `${box.height + 12}px`,
    });
    document.body.appendChild(ring);
    setTimeout(() => ring.remove(), duration);
  }, { box, duration: durationMs });
  await page.waitForTimeout(durationMs);
}

async function tutorialClick(page, visualTarget, settleMs = 550, clickTarget = visualTarget) {
  await moveTutorialCursor(page, visualTarget, 250);
  await page.evaluate(() => document.getElementById("_demo_cursor")?.classList.add("clicking"));
  await page.waitForTimeout(160);
  await clickTarget.click({ timeout: 3000 });
  await page.evaluate(() => document.getElementById("_demo_cursor")?.classList.remove("clicking"));
  await page.waitForTimeout(settleMs);
}

async function hideTutorialCursor(page) {
  await page.evaluate(() => {
    const cursor = document.getElementById("_demo_cursor");
    if (cursor) cursor.style.opacity = "0";
    document.getElementById("_demo_cursor_ring")?.remove();
  });
  await page.waitForTimeout(250);
}

async function typeMessage(page, text) {
  const input = page.locator(target("owui.chat-input"));
  await input.waitFor({ state: "visible", timeout: 15000 });
  await input.evaluate((element) => {
    element.focus();
    element.innerHTML = "<p></p>";
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
  for (const character of text) {
    await input.evaluate((element, value) => {
      const paragraph = element.querySelector("p") || element;
      paragraph.textContent += value;
      element.dispatchEvent(new Event("input", { bubbles: true }));
    }, character);
    await page.waitForTimeout(7);
  }
}

async function sendMessage(page) {
  const send = page.locator(target("owui.send"));
  await send.waitFor({ state: "visible", timeout: 10000 });
  await send.click();
}

async function waitForResponse(page, timeoutMs) {
  const startedAt = Date.now();
  const startDeadline = startedAt + Math.min(timeoutMs, 20000);
  const completionDeadline = startedAt + timeoutMs;
  let stable = 0;
  while (Date.now() < startDeadline) {
    const state = await page.evaluate(() => ({
      voice: Boolean(document.getElementById("voice-input-button")),
      stop: Boolean(document.getElementById("stop-response-button")
        || document.querySelector('[aria-label="Stop"],button[id*="stop"]')),
      generating: Boolean(document.querySelector(".generating,.thinking,[data-generating]")),
    }));
    if (!state.voice || state.stop || state.generating) break;
    await page.waitForTimeout(250);
  }
  if (Date.now() >= startDeadline) {
    throw new Error("Generation did not start within 20 seconds");
  }
  log("generation", `started after ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);

  while (Date.now() < completionDeadline) {
    const state = await page.evaluate(() => ({
      voice: Boolean(document.getElementById("voice-input-button")),
      stop: Boolean(document.getElementById("stop-response-button")
        || document.querySelector('[aria-label="Stop"],button[id*="stop"]')),
      generating: Boolean(document.querySelector(".generating,.thinking,[data-generating]")),
    }));
    if (state.voice && !state.stop && !state.generating) {
      stable += 1;
      if (stable >= 2) {
        log("generation", `completed after ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
        return;
      }
    } else {
      stable = 0;
    }
    await page.waitForTimeout(1000);
  }
  throw new Error(`Generation did not complete within ${timeoutMs}ms`);
}

async function enableLathe(page, { tutorial = false } = {}) {
  let menuSelector;
  for (const selector of TARGETS["owui.integration-menu"]) {
    if (await page.locator(selector).isVisible().catch(() => false)) {
      menuSelector = selector;
      break;
    }
  }
  if (!menuSelector) throw new Error("Could not find the logical Open WebUI integration menu target");
  const menu = page.locator(menuSelector);
  if (tutorial) {
    await installTutorialCursor(page);
    await tutorialClick(page, menu, 650);
  } else {
    await menu.click();
  }
  const tools = page.getByRole("button", { name: /^Tools \d+$/ }).last();
  await tools.waitFor({ state: "visible", timeout: 5000 });
  if (tutorial) await tutorialClick(page, tools, 650);
  else await tools.click();
  const row = page.getByRole("button", { name: /Lathe$/ }).last();
  await row.waitFor({ state: "visible", timeout: 5000 });
  const toggle = row.locator('button[role="switch"]');
  await toggle.waitFor({ state: "visible", timeout: 5000 });
  if (tutorial) {
    await moveTutorialCursor(page, toggle, 450);
    await emphasizeTutorialTarget(page, row, 850);
  }
  if (await toggle.getAttribute("aria-checked") !== "true") {
    if (tutorial) await tutorialClick(page, toggle, 900, row);
    else await row.click({ timeout: 3000 });
  } else if (tutorial) {
    await page.waitForTimeout(900);
  }
  await page.keyboard.press("Escape");
  if (tutorial) await hideTutorialCursor(page);
  return menuSelector;
}

async function scrollChatToBottom(page) {
  await page.evaluate(() => {
    const candidates = [...document.querySelectorAll("div,main")]
      .filter((item) => item.scrollHeight > item.clientHeight && item.clientHeight > 200)
      .sort((a, b) => b.scrollHeight - a.scrollHeight);
    candidates[0]?.scrollTo({ top: candidates[0].scrollHeight, behavior: "instant" });
  });
  await page.waitForTimeout(400);
}

async function visibleButton(page, pattern) {
  const buttons = page.getByRole("button", { name: pattern });
  for (let index = 0; index < await buttons.count(); index++) {
    const button = buttons.nth(index);
    if (await button.isVisible().catch(() => false)) return button;
  }
  return null;
}

async function failUnknownAuth(page, flow) {
  const snapshot = await page.evaluate(() => ({
    title: document.title,
    text: document.body?.innerText.slice(0, 1200) || "",
  })).catch(() => ({ title: "", text: "" }));
  throw new Error(`${flow} authentication reached an unknown state at ${page.url()}: ${snapshot.title}\n${snapshot.text}`);
}

async function ensureOwuiAuth(page) {
  await page.goto(`${OWUI_URL}/auth`);
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const url = new URL(page.url());
    const token = await page.evaluate(() => localStorage.getItem("token")).catch(() => null);
    if (url.origin === OWUI_URL && token && !url.pathname.startsWith("/auth")) return token;
    const pattern = url.origin === OWUI_URL
      ? /continue|sign in|log in|single sign-on|sso/i
      : /sign in|continue|authorize|allow|approve/i;
    const action = await visibleButton(page, pattern);
    if (action) {
      log("auth", `${url.origin}: ${JSON.stringify(await action.innerText())}`);
      await action.click();
    } else {
      await page.waitForTimeout(750);
    }
  }
  await failUnknownAuth(page, "Open WebUI");
}

async function ensurePreviewAuth(page, previewOrigin) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    if (await page.locator(target("editor.workbench")).isVisible().catch(() => false)) return;
    const url = new URL(page.url());
    const action = await visibleButton(page, /sign in|continue|authorize|allow|approve/i);
    if (action) {
      log("preview-auth", `${url.origin}: ${JSON.stringify(await action.innerText())}`);
      await action.click();
    } else if (url.origin === previewOrigin || url.pathname.includes("callback")) {
      await page.waitForTimeout(1000);
    } else {
      await page.waitForTimeout(750);
    }
  }
  await failUnknownAuth(page, "Private preview");
}

function chatIdFromUrl(url) {
  return url?.match(/\/c\/([a-f0-9-]+)/i)?.[1] || null;
}

async function fetchChat(state) {
  const chatId = chatIdFromUrl(state.chatUrl || state.page?.url());
  if (!chatId || !state.token) return null;
  const response = await fetch(`${OWUI_URL}/api/v1/chats/${chatId}`, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  if (!response.ok) throw new Error(`Could not read chat history: HTTP ${response.status}`);
  return response.json();
}

function contains(value, expected) {
  return !expected || JSON.stringify(value).toLowerCase().includes(expected.toLowerCase());
}

async function awaitToolResult(state, expected) {
  const deadline = Date.now() + 4000;
  let observed = [];
  while (Date.now() < deadline) {
    const events = normalizedToolEvents(await fetchChat(state));
    observed = events.slice(state.toolBaseline);
    const match = observed.find((event) =>
      expected.tools.includes(event.tool)
      && contains(event.arguments, expected.argumentContains)
      && contains(event.output, expected.outputContains));
    if (match) return match;
    await state.page.waitForTimeout(500);
  }
  const summary = observed.length
    ? observed.map((event) => `${event.tool}:${event.output ? "output" : "no-output"}`).join(", ")
    : "none";
  throw new Error(
    `No matching ${expected.tools.join("/")} tool result appeared after the completed turn; observed: ${summary}`,
  );
}

function findExposeUrl(page) {
  return page.evaluate((domains) => {
    for (const anchor of document.querySelectorAll("a[href]")) {
      try {
        const url = new URL(anchor.getAttribute("href"));
        const host = url.hostname.toLowerCase();
        if (url.protocol === "https:" && !url.username && !url.password
          && domains.some((domain) => host === domain || host.endsWith(`.${domain}`))) return url.href;
      } catch {}
    }
    return null;
  }, ALLOWED_PROXY_DOMAINS);
}

async function openPreview(state) {
  if (state.previewReady) return;
  const url = await findExposeUrl(state.page);
  if (!url) throw new Error("No trusted HTTPS preview URL found in the response");
  await state.page.goto(url);
  await ensurePreviewAuth(state.page, new URL(url).origin);
  state.previewReady = true;
}

async function ensureTerminal(page) {
  const terminal = page.locator(target("editor.terminal")).first();
  if (!await terminal.isVisible().catch(() => false)) {
    await page.keyboard.press("Escape");
    await page.keyboard.press("Control+`");
    await terminal.waitFor({ state: "visible", timeout: 5000 });
  }
  const input = page.locator("textarea.xterm-helper-textarea,.xterm textarea").last();
  await input.waitFor({ state: "attached", timeout: 3000 });
  await input.click({ force: true });
  await page.waitForTimeout(250);
  return input;
}

function redact(value) {
  if (typeof value === "string") {
    return value
      .replace(/Bearer\s+[A-Za-z0-9._~+\/-]+/gi, "Bearer [REDACTED]")
      .replace(/https:\/\/[^\s"'<>]+/g, (url) => {
        try { return `${new URL(url).origin}/[REDACTED]`; } catch { return "[REDACTED_URL]"; }
      })
      .replace(/[A-Za-z0-9_-]{80,}/g, "[REDACTED_TOKEN]");
  }
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object") {
    const sensitiveRecord = typeof value.name === "string"
      && /token|cookie|secret|passkey|authorization|privatekey/i.test(value.name);
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      /token|cookie|secret|passkey|authorization|privatekey/i.test(key)
        || (sensitiveRecord && key === "value") ? "[REDACTED]" : redact(item),
    ]));
  }
  return value;
}

function reportedModels(payload) {
  const chat = payload?.chat || payload || {};
  const values = [chat.models, chat.model, chat.model_id, payload?.models, payload?.model];
  return [...new Set(values.flat().filter((value) => typeof value === "string" && value))];
}

function isImage(bytes) {
  return bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47
    || bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff
    || String.fromCharCode(...bytes.slice(0, 4)) === "RIFF";
}

async function sanitizeTrace(path) {
  const archive = unzipSync(new Uint8Array(await readFile(path)));
  for (const [name, bytes] of Object.entries(archive)) {
    if (name.endsWith(".trace") || name.endsWith(".network") || name.endsWith(".stacks")) {
      const sanitized = strFromU8(bytes).split("\n").map((line) => {
        if (!line) return line;
        try { return JSON.stringify(redact(JSON.parse(line))); } catch { return redact(line); }
      }).join("\n");
      archive[name] = strToU8(sanitized);
    } else if (name.startsWith("resources/") && !isImage(bytes)) {
      delete archive[name];
    }
  }
  await writeFile(path, zipSync(archive));
}

let browser;
try {
  browser = await chromium.launch({ headless: true });
} catch (error) {
  const now = new Date().toISOString();
  await writeFile(resolve(OUT_DIR, "capture-report.json"), `${JSON.stringify({
    schema_version: 1,
    scenario: SCENARIO.id,
    scenario_version: SCENARIO.version,
    status: "rejected",
    failure_domain: "infrastructure",
    failed_step: "browser.launch",
    error: error.message,
    started_at: now,
    completed_at: now,
    steps: [],
  }, null, 2)}\n`);
  throw error;
}
const state = {
  browser,
  variables: { checkout: "/home/daytona/workspace/lathe-demo" },
  loginContext: null,
  context: null,
  page: null,
  token: null,
  cookies: [],
  chatUrl: null,
  toolBaseline: 0,
  previewReady: false,
  editorRoute: null,
  finalChat: null,
  releaseScreenshots: [],
  onEvent: async (event, report) => {
    const detail = event.error ? `: ${event.error}` : "";
    log(event.step || "scenario", `${event.phase}${event.operation ? ` (${event.operation})` : ""}${detail}`);
    if (state.page && event.phase === "step-passed" && event.screenshot) {
      const name = `${String(state.releaseScreenshots.length + 1).padStart(2, "0")}-${event.screenshot}.png`;
      await state.page.waitForTimeout(250);
      await state.page.screenshot({ path: resolve(OUT_DIR, "screenshots", name) });
      state.releaseScreenshots.push(name);
    } else if (state.page && event.phase === "step-failed") {
      const name = `${event.step}-${event.attempt}-${event.operation}`.replace(/[^a-z0-9._-]+/gi, "-");
      await state.page.screenshot({
        path: resolve(OUT_DIR, "diagnostics", `${name}.png`),
      }).catch(() => {});
    }
    const snapshot = {
      ...report,
      repository_commit: process.env.GITHUB_SHA || null,
      configured_model: MODEL,
      release_screenshots: state.releaseScreenshots,
    };
    await writeFile(
      resolve(OUT_DIR, "capture-report.json"),
      `${JSON.stringify(redact(snapshot), null, 2)}\n`,
    );
  },
};

const adapter = {
  "auth.ensure-owui": async () => {
    state.loginContext = await browser.newContext({ viewport: VIEWPORT });
    await state.loginContext.credentials.create(PASSKEY.rpId, PASSKEY);
    await state.loginContext.credentials.install();
    state.loginPage = await state.loginContext.newPage();
    state.token = await ensureOwuiAuth(state.loginPage);
    state.cookies = await state.loginContext.cookies();
    return { observations: ["auth.state=owui-authenticated"] };
  },

  "sandbox.prewarm": async () => {
    if (process.env.DEMO_SKIP_PREWARM === "1") {
      return { result: "skipped", observations: ["sandbox.prewarm=skipped-local"] };
    }
    const page = state.loginPage;
    await page.goto(CHAT_URL);
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.keyboard.press("Escape");
    await suppressIncidentalUi(page);
    await page.locator(target("owui.chat-input")).waitFor({ state: "visible", timeout: 15000 });
    await enableLathe(page);
    await typeMessage(page, "Run exactly: mkdir -p /home/daytona/workspace/.vscode && printf '%s\\n' '{\"chat.disableAIFeatures\":true,\"workbench.secondarySideBar.defaultVisibility\":\"hidden\"}' > /home/daytona/workspace/.vscode/settings.json && rm -rf /home/daytona/workspace/lathe-demo && echo warm");
    await sendMessage(page);
    await waitForResponse(page, 120000);
    const chatId = chatIdFromUrl(page.url());
    if (chatId) await fetch(`${OWUI_URL}/api/v1/chats/${chatId}`, {
      method: "DELETE", headers: { Authorization: `Bearer ${state.token}` },
    });
    return { observations: ["sandbox.state=ready"] };
  },

  "capture.start": async () => {
    await state.loginContext.close();
    state.loginContext = null;
    state.context = await browser.newContext({
      viewport: VIEWPORT,
      recordVideo: { dir: VIDEO_TMP, size: VIEWPORT },
    });
    await state.context.tracing.start({ screenshots: true, snapshots: true, sources: false });
    await state.context.credentials.create(PASSKEY.rpId, PASSKEY);
    await state.context.credentials.install();
    await state.context.addCookies(state.cookies);
    state.page = await state.context.newPage();
    await state.page.goto(`${OWUI_URL}/auth`);
    await state.page.evaluate((token) => localStorage.setItem("token", token), state.token);
    return { observations: ["capture.state=recording"] };
  },

  "chat.open": async () => {
    await state.page.goto(CHAT_URL);
    await state.page.waitForLoadState("networkidle").catch(() => {});
    await state.page.keyboard.press("Escape");
    await suppressIncidentalUi(state.page);
    await state.page.locator(target("owui.chat-input")).waitFor({ state: "visible", timeout: 15000 });
    await state.page.waitForTimeout(1200);
    return { observations: ["chat.state=fresh"] };
  },

  "owui.enable-tool": async () => ({
    observations: [`owui.integration-menu=${await enableLathe(state.page, { tutorial: true })}`, "owui.tool.Lathe=enabled"],
  }),

  "chat.send": async ({ prompt, timeoutMs }) => {
    state.toolBaseline = normalizedToolEvents(await fetchChat(state).catch(() => null)).length;
    await scrollChatToBottom(state.page);
    await typeMessage(state.page, prompt);
    await sendMessage(state.page);
    await waitForResponse(state.page, timeoutMs);
    state.chatUrl = state.page.url();
    return { observations: ["chat.state=idle"] };
  },

  "chat.await-tool-result": async (expected) => {
    const event = await awaitToolResult(state, expected);
    return { observations: [`chat.tool=${event.tool}`, "chat.tool.status=completed"] };
  },

  "editor.open-via-explorer": async ({ path }) => {
    await openPreview(state);
    await state.page.keyboard.press("Control+Shift+E");
    const folder = state.page.getByText("lathe-demo", { exact: true }).last();
    await folder.waitFor({ state: "visible", timeout: 10000 });
    await folder.click();
    await state.page.keyboard.press("ArrowRight");
    const file = state.page.getByText("RELAY.md", { exact: true }).last();
    await file.waitFor({ state: "visible", timeout: 5000 });
    await file.dblclick();
    await state.page.waitForTimeout(750);
    state.editorRoute = "explorer";
    return { observations: [`editor.open-path=${path}`, "editor.route=explorer"] };
  },

  "editor.open-via-quick-open": async ({ path }) => {
    await openPreview(state);
    await state.page.keyboard.press("Control+P");
    const quickInput = state.page.locator(".quick-input-widget input").first();
    await quickInput.waitFor({ state: "visible", timeout: 1500 });
    await quickInput.fill(path);
    await quickInput.press("Enter");
    await state.page.getByText("RELAY.md", { exact: true }).last().waitFor({ state: "visible", timeout: 10000 });
    await state.page.waitForTimeout(750);
    state.editorRoute = "quick-open";
    return { observations: [`editor.open-path=${path}`, "editor.route=quick-open"] };
  },

  "editor.open-via-terminal": async ({ path }) => {
    await openPreview(state);
    const input = await ensureTerminal(state.page);
    await input.pressSequentially(`sed -n '1,2p' ${path}`, { delay: 15 });
    await input.press("Enter");
    await state.page.waitForTimeout(1200);
    state.editorRoute = "terminal";
    return { observations: [`editor.open-path=${path}`, "editor.route=terminal"] };
  },

  "editor.replace-line": async ({ line, text }) => {
    if (state.editorRoute === "terminal") {
      throw new Error("The terminal presentation route requires the terminal edit fallback");
    }
    const editorInput = state.page.locator(".monaco-editor textarea,textarea.inputarea").last();
    await editorInput.waitFor({ state: "attached", timeout: 2500 });
    await editorInput.click({ force: true });
    await state.page.keyboard.press("Control+G");
    await state.page.keyboard.type(String(line));
    await state.page.keyboard.press("Enter");
    await state.page.keyboard.press("Home");
    await state.page.keyboard.press("Shift+End");
    await state.page.keyboard.type(text, { delay: 55 });
    await state.page.keyboard.press("Control+S");
    await state.page.waitForTimeout(1200);
    return { observations: [`editor.replaced-line=${line}`] };
  },

  "editor.replace-line-via-terminal": async ({ line, text }) => {
    const input = await ensureTerminal(state.page);
    const path = `${state.variables.checkout}/RELAY.md`;
    const code = `from pathlib import Path; p=Path(${JSON.stringify(path)}); xs=p.read_text().splitlines(); xs[${line - 1}]=${JSON.stringify(text)}; p.write_text('\\n'.join(xs)+'\\n')`;
    const encoded = Buffer.from(code).toString("base64");
    await input.pressSequentially(`python3 -c "$(printf %s ${encoded} | base64 -d)"`, { delay: 8 });
    await input.press("Enter");
    await state.page.waitForTimeout(1000);
    return { observations: [`editor.replaced-line=${line}`, "editor.route=terminal"] };
  },

  "chat.return": async () => {
    await state.page.goto(state.chatUrl);
    await state.page.waitForLoadState("networkidle").catch(() => {});
    await suppressIncidentalUi(state.page);
    await scrollChatToBottom(state.page);
    await enableLathe(state.page);
    return { observations: ["chat.state=restored"] };
  },

  "camera.hold": async ({ milliseconds }) => {
    await scrollChatToBottom(state.page);
    await state.page.waitForTimeout(milliseconds);
    return { observations: ["camera.state=held"] };
  },
};

let report;
let captureError;
try {
  report = await runScenario(SCENARIO, adapter, state);
  report.repository_commit = process.env.GITHUB_SHA || null;
  report.configured_model = MODEL;
  report.release_screenshots = state.releaseScreenshots;
  state.finalChat = await fetchChat(state).catch(() => null);
  report.reported_models = reportedModels(state.finalChat);
  log("qualification", "accepted");
  const chatId = chatIdFromUrl(state.chatUrl);
  if (chatId) await fetch(`${OWUI_URL}/api/v1/chats/${chatId}`, {
    method: "DELETE", headers: { Authorization: `Bearer ${state.token}` },
  }).catch((error) => log("cleanup", error.message));
} catch (error) {
  captureError = error;
  report = error.report || {
    schema_version: 1,
    scenario: SCENARIO.id,
    scenario_version: SCENARIO.version,
    status: "rejected",
    failure_domain: "infrastructure",
    error: error.message,
    steps: [],
  };
  report.repository_commit = process.env.GITHUB_SHA || null;
  report.configured_model = MODEL;
  report.release_screenshots = state.releaseScreenshots;
  state.finalChat = await fetchChat(state).catch(() => null);
  report.reported_models = reportedModels(state.finalChat);
  log("qualification", `rejected: ${error.message}`);
} finally {
  const diagnosticPage = state.page || state.loginPage;
  if (diagnosticPage) {
    await diagnosticPage.screenshot({ path: resolve(OUT_DIR, "last-page.png"), fullPage: true }).catch(() => {});
    state.finalChat ||= await fetchChat(state).catch(() => null);
    if (state.finalChat) {
      await writeFile(resolve(OUT_DIR, "sanitized-chat.json"), `${JSON.stringify(redact(state.finalChat), null, 2)}\n`);
    }
  }
  await writeFile(resolve(OUT_DIR, "capture-report.json"), `${JSON.stringify(redact(report), null, 2)}\n`);
  if (state.context) {
    const tracePath = resolve(OUT_DIR, "playwright-trace.zip");
    await state.context.tracing.stop({ path: tracePath }).catch(() => {});
    await sanitizeTrace(tracePath).catch((error) => {
      log("trace", `Sanitization failed, removing trace from upload set: ${error.message}`);
      return import("node:fs/promises").then(({ rm }) => rm(tracePath, { force: true }));
    });
    const video = state.page?.video();
    await state.context.close();
    const videoPath = await video?.path().catch(() => null);
    if (videoPath) await rename(videoPath, resolve(OUT_DIR, "demo.webm"));
  }
  if (state.loginContext) await state.loginContext.close().catch(() => {});
  await browser.close();
}

if (captureError) throw captureError;
