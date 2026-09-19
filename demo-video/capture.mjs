#!/usr/bin/env node
/**
 * Playwright video capture of a real Lathe session on an Open WebUI instance.
 *
 * Captures: login → pre-warm sandbox → enable Lathe → clone and inspect
 * a repo → edit a shared relay file in VS Code → read it back in chat →
 * background a focused review while the main agent stops the IDE.
 *
 * Usage:  node capture.mjs
 * Env:    DEMO_OWUI_URL, DEMO_PASSKEY, DEMO_MODEL or OWUI_MODEL
 * Output: out/demo.webm
 */

import { chromium } from "playwright";
import { readFileSync, mkdirSync } from "fs";
import { rename } from "fs/promises";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "out");
mkdirSync(OUT_DIR, { recursive: true });

// Load .env from repo root (local dev only — CI uses secrets)
try {
  for (const line of readFileSync(resolve(__dirname, "..", ".env"), "utf-8").split("\n")) {
    const m = line.match(/^([A-Za-z_]\w*)=(.*)$/);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2];
  }
} catch {}

const OWUI_URL = (process.env.DEMO_OWUI_URL || "").replace(/\/+$/, "");
const MODEL = process.env.DEMO_MODEL || process.env.OWUI_MODEL;
let PASSKEY;
try {
  PASSKEY = JSON.parse(process.env.DEMO_PASSKEY || "");
} catch {}
if (!OWUI_URL || !PASSKEY?.rpId || !PASSKEY?.id || !PASSKEY?.userHandle || !PASSKEY?.privateKey || !PASSKEY?.publicKey || !MODEL) {
  console.error("Set DEMO_OWUI_URL, DEMO_PASSKEY, and DEMO_MODEL");
  process.exit(1);
}
const CHAT_URL = `${OWUI_URL}/?model=${encodeURIComponent(MODEL)}`;
const ALLOWED_PROXY_DOMAINS = (process.env.DEMO_PROXY_DOMAINS || "daytonaproxy01.net,proxy.app.daytona.io,proxy.daytona.work")
  .split(",")
  .map((domain) => domain.trim().toLowerCase())
  .filter(Boolean);

const VIEWPORT = { width: 1280, height: 720 };


// ── Structured logging ──────────────────────────────────────────────

const t0 = Date.now();
function log(beat, msg) {
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[${elapsed}s] ${beat}: ${msg}`);
}

// ── Helpers ──────────────────────────────────────────────────────────

async function suppressTooltips(page) {
  await page.evaluate(() => {
    const s = document.createElement("style");
    s.textContent = [
      `[data-tooltip]:before,[data-tooltip]:after,.tooltip,[role=tooltip]`,
      `{display:none!important;visibility:hidden!important}`,
      `#model-selector-model-button`,
      `{visibility:hidden!important}`,
    ].join("");
    document.head.appendChild(s);

    // The empty-chat hero repeats the selected model at headline scale. Model
    // identity is incidental to this demo, so remove that row rather than let
    // a long deployment-specific name dominate the opening composition.
    const modelInfo = document.querySelector('button[aria-label^="Get information on "]');
    if (modelInfo?.parentElement?.parentElement) {
      modelInfo.parentElement.parentElement.style.visibility = "hidden";
    }
    const modelName = modelInfo?.getAttribute("aria-label")
      ?.match(/^Get information on (.+) in the UI$/)?.[1];
    if (modelName) {
      for (const el of document.querySelectorAll("*")) {
        if (el.children.length === 0 && el.textContent.trim() === modelName) {
          el.style.visibility = "hidden";
        }
      }
    }
  });
}

// ── Cursor overlay ──────────────────────────────────────────────────

async function injectCursor(page) {
  await page.evaluate(() => {
    if (document.getElementById("_capture_cursor")) return;
    const dot = document.createElement("div");
    dot.id = "_capture_cursor";
    Object.assign(dot.style, {
      position: "fixed",
      zIndex: "999999",
      width: "20px",
      height: "20px",
      borderRadius: "50%",
      background: "rgba(59, 130, 246, 0.7)",
      border: "2px solid rgba(59, 130, 246, 0.9)",
      boxShadow: "0 0 8px rgba(59, 130, 246, 0.4)",
      pointerEvents: "none",
      transition: "left 0.4s ease, top 0.4s ease, transform 0.15s ease, opacity 0.3s ease",
      transform: "translate(-50%, -50%)",
      left: "-40px",
      top: "-40px",
      opacity: "0",
    });
    document.body.appendChild(dot);
  });
}

async function cursorTo(page, selector) {
  await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const dot = document.getElementById("_capture_cursor");
    if (!dot) return;
    dot.style.opacity = "1";
    dot.style.left = `${r.left + r.width / 2}px`;
    dot.style.top = `${r.top + r.height / 2}px`;
  }, selector);
  await page.waitForTimeout(400);
}

async function cursorClick(page, selector) {
  await cursorTo(page, selector);
  await page.evaluate(() => {
    const dot = document.getElementById("_capture_cursor");
    if (dot) {
      dot.style.transform = "translate(-50%, -50%) scale(1.5)";
      setTimeout(() => { dot.style.transform = "translate(-50%, -50%) scale(1)"; }, 150);
    }
  });
  await page.waitForTimeout(200);
}

async function cursorHide(page) {
  await page.evaluate(() => {
    const dot = document.getElementById("_capture_cursor");
    if (dot) dot.style.opacity = "0";
  });
}

// ── Highlight ring ──────────────────────────────────────────────────
// Draws a pulsing outline around a DOM element to draw the viewer's eye
// before an interaction. More visible than the cursor dot for calling
// out specific UI regions (buttons, links, text areas).

async function highlight(page, selector, durationMs = 1500) {
  await page.evaluate(({ sel, ms }) => {
    const el = document.querySelector(sel);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const ring = document.createElement("div");
    ring.className = "_capture_highlight";
    const pad = 6;
    Object.assign(ring.style, {
      position: "fixed",
      zIndex: "999998",
      left: `${r.left - pad}px`,
      top: `${r.top - pad}px`,
      width: `${r.width + pad * 2}px`,
      height: `${r.height + pad * 2}px`,
      borderRadius: "8px",
      border: "2.5px solid rgba(59, 130, 246, 0.85)",
      boxShadow: "0 0 12px rgba(59, 130, 246, 0.4)",
      pointerEvents: "none",
      animation: "_capture_pulse 0.8s ease-in-out infinite alternate",
      opacity: "1",
      transition: "opacity 0.3s ease",
    });
    // Inject keyframes if not already present
    if (!document.getElementById("_capture_pulse_style")) {
      const style = document.createElement("style");
      style.id = "_capture_pulse_style";
      style.textContent = `@keyframes _capture_pulse { from { box-shadow: 0 0 6px rgba(59,130,246,0.3); } to { box-shadow: 0 0 18px rgba(59,130,246,0.6); } }`;
      document.head.appendChild(style);
    }
    document.body.appendChild(ring);
    setTimeout(() => {
      ring.style.opacity = "0";
      setTimeout(() => ring.remove(), 300);
    }, ms);
  }, { sel: selector, ms: durationMs });
  await page.waitForTimeout(durationMs);
}

// ── URL extraction ──────────────────────────────────────────────────
// The prompt asks the model to use a markdown link (e.g. [Open VS Code](...))
// so the raw URL never appears as visible text — only in <a> href attrs.
// We extract the real URL from the DOM to navigate to it.

async function findExposeUrl(page) {
  const hrefs = await page.evaluate(() =>
    [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href") || ""),
  );
  for (const href of hrefs) {
    let url;
    try {
      url = new URL(href);
    } catch {
      continue;
    }
    const hostname = url.hostname.toLowerCase();
    const trusted = ALLOWED_PROXY_DOMAINS.some(
      (domain) => hostname === domain || hostname.endsWith(`.${domain}`),
    );
    if (trusted && url.protocol === "https:" && !url.username && !url.password) {
      return url.href;
    }
  }
  return null;
}

// ── Chat interaction helpers ────────────────────────────────────────

async function typeMessage(page, text) {
  await page.evaluate(() => {
    const input = document.getElementById("chat-input");
    if (!input) return;
    input.focus();
    input.innerHTML = "<p></p>";
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  for (const ch of text) {
    await page.evaluate((c) => {
      const input = document.getElementById("chat-input");
      if (!input) return;
      const p = input.querySelector("p") || input;
      p.textContent += c;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, ch);
    await page.waitForTimeout(10);
  }
}

async function sendMessage(page) {
  try {
    await page.waitForSelector("#send-message-button", { state: "visible", timeout: 3000 });
  } catch {
    log("sendMessage", "Send button not visible after 3s — clicking anyway");
  }
  await page.click("#send-message-button");
}

async function enableLathe(page, { animate = false } = {}) {
  const intBtn = await page.evaluate(() => {
    for (const sel of ["#integration-menu-button", "#tools-menu-button"])
      if (document.querySelector(sel)) return sel;
    return null;
  });
  if (!intBtn) throw new Error("Could not find integration menu button");

  if (animate) await cursorClick(page, intBtn);
  await page.click(intBtn);

  const toolsRow = page.getByRole("button", { name: /^Tools \d+$/ }).last();
  await toolsRow.waitFor({ state: "visible", timeout: 5000 });
  if (animate) {
    await toolsRow.evaluate((el) => el.setAttribute("data-capture-tools-row", "1"));
    await cursorClick(page, "[data-capture-tools-row]");
  }
  await toolsRow.click();

  const latheRow = page.getByRole("button", { name: /Lathe$/ }).last();
  await latheRow.waitFor({ state: "visible", timeout: 5000 });
  const latheToggle = latheRow.locator('button[role="switch"]');
  await latheToggle.waitFor({ state: "visible", timeout: 5000 });
  await latheToggle.evaluate((el) =>
    el.setAttribute("data-capture-lathe-toggle", "1"),
  );

  if (animate) await cursorClick(page, "[data-capture-lathe-toggle]");
  const enabled = await page.getAttribute(
    "[data-capture-lathe-toggle]",
    "aria-checked",
  ) === "true";
  if (!enabled) await latheRow.click();
  await page.keyboard.press("Escape");
  return intBtn;
}

/**
 * Wait for the model to finish responding.
 */
async function waitForResponse(page, { timeoutMs = 180000, stableMs = 5000 } = {}) {
  const startTime = Date.now();
  const deadline = startTime + timeoutMs;
  const stableChecks = Math.ceil(stableMs / 1000);

  // Phase 1: wait for generation to start
  let generationStarted = false;
  while (Date.now() < deadline) {
    const state = await page.evaluate(() => {
      const hasVoice = !!document.getElementById("voice-input-button");
      const hasStop = !!document.getElementById("stop-response-button")
        || !!document.querySelector('[aria-label="Stop"]')
        || !!document.querySelector('button[id*="stop"]');
      return { hasVoice, hasStop };
    });
    if (!state.hasVoice || state.hasStop) {
      generationStarted = true;
      break;
    }
    await page.waitForTimeout(250);
  }
  log("waitForResponse", generationStarted
    ? `Generation started (${((Date.now() - startTime) / 1000).toFixed(1)}s)`
    : `Timed out waiting for generation to start`);
  if (!generationStarted) throw new Error(`Generation did not start within ${timeoutMs}ms`);

  // Phase 2: wait for generation to end (stable idle)
  let stableCount = 0;
  while (Date.now() < deadline) {
    const state = await page.evaluate(() => {
      const hasVoice = !!document.getElementById("voice-input-button");
      const hasStop = !!document.getElementById("stop-response-button")
        || !!document.querySelector('[aria-label="Stop"]')
        || !!document.querySelector('button[id*="stop"]');
      const hasGenerating = !!document.querySelector(".generating, .thinking, [data-generating]");
      return { hasVoice, hasStop, hasGenerating };
    });
    const isIdle = state.hasVoice && !state.hasStop && !state.hasGenerating;
    if (isIdle) {
      stableCount++;
      if (stableCount >= stableChecks) {
        log("waitForResponse", `Generation complete (${((Date.now() - startTime) / 1000).toFixed(0)}s, stable for ${stableMs}ms)`);
        return;
      }
    } else {
      if (stableCount > 0) {
        log("waitForResponse", `Stability reset at count=${stableCount} (voice=${state.hasVoice}, stop=${state.hasStop})`);
      }
      stableCount = 0;
    }
    await page.waitForTimeout(1000);
  }
  log("waitForResponse", `Timed out after ${((Date.now() - startTime) / 1000).toFixed(0)}s`);
  throw new Error(`Generation did not complete within ${timeoutMs}ms`);
}

// ── Chat scrolling ──────────────────────────────────────────────────

async function scrollChat(page, deltaY, smooth = true) {
  await page.evaluate(({ dy, smooth }) => {
    let container = document.querySelector("[data-capture-scroll]");
    if (!container) {
      const candidates = document.querySelectorAll("div, main");
      let best = null, bestH = 0;
      for (const c of candidates) {
        if (c.scrollHeight > c.clientHeight && c.clientHeight > 200 && c.scrollHeight > bestH) {
          best = c; bestH = c.scrollHeight;
        }
      }
      if (best) { best.setAttribute("data-capture-scroll", "1"); container = best; }
    }
    if (container) container.scrollBy({ top: dy, behavior: smooth ? "smooth" : "instant" });
  }, { dy: deltaY, smooth });
}

/** Scroll to the bottom of the chat (to see latest content). */
async function scrollToBottom(page) {
  await scrollChat(page, 99999, false);
  await page.waitForTimeout(500);
}

// ── Main ─────────────────────────────────────────────────────────────

// Write Playwright's raw video to /tmp so UUID-named intermediate files
// don't spill into the working tree (they linger on crash/abort).
const VIDEO_TMP = "/tmp/capture-video";
mkdirSync(VIDEO_TMP, { recursive: true });

const browser = await chromium.launch({ headless: true });

// ── Login in an unrecorded context ──────────────────────────────
// Auth is independent of Lathe — no reason to show it in the video.
log("login", `Navigating to ${OWUI_URL}...`);
const loginContext = await browser.newContext({ viewport: VIEWPORT });
await loginContext.credentials.create(PASSKEY.rpId, PASSKEY);
await loginContext.credentials.install();
const loginPage = await loginContext.newPage();
await loginPage.goto(`${OWUI_URL}/auth`);
await loginPage.waitForLoadState("networkidle").catch(() => {});
await loginPage.waitForTimeout(500);

if (loginPage.url().startsWith(OWUI_URL)) {
  await loginPage.getByRole("button", { name: "Continue with Pocket ID", exact: true }).click();
  await loginPage.waitForURL((url) => url.origin !== OWUI_URL, { timeout: 30000 });
}
log("login", "Signing in...");
await loginPage.getByRole("button", { name: "Sign in", exact: true }).click();
await loginPage.waitForURL((url) => url.origin === OWUI_URL && !url.pathname.startsWith("/auth"), { timeout: 30000 });

// Dismiss "What's New" modal if present
await loginPage.evaluate(() => {
  for (const b of document.querySelectorAll("button"))
    if (b.textContent.trim() === "Okay, Let's Go!") b.click();
});
await loginPage.waitForTimeout(500);
log("login", "Logged in");

// Extract auth token from localStorage (OWUI stores JWT there, not in cookies)
const token = await loginPage.evaluate(() => localStorage.getItem("token"));
const cookies = await loginContext.cookies();
log("login", token ? "Authentication token acquired" : "Authentication token missing");
if (!token) throw new Error("Open WebUI login did not produce an authentication token");

// ── Pre-warm: wake the sandbox before recording starts ──────────
// Send a trivial Lathe-triggering message in the unrecorded browser
// so the VM is hot by the time the recorded session begins.
log("prewarm", "Waking sandbox...");
try {
  await loginPage.goto(CHAT_URL);
  await loginPage.waitForLoadState("networkidle").catch(() => {});
  await loginPage.waitForTimeout(500);

  // Dismiss "What's New" modal again if it reappears
  await loginPage.evaluate(() => {
    for (const b of document.querySelectorAll("button"))
      if (b.textContent.trim() === "Okay, Let's Go!") b.click();
  });

  // Enable Lathe in this throwaway chat.
  await enableLathe(loginPage);

  // Send a trivial message that triggers a tool call
  await loginPage.evaluate(() => {
    const input = document.getElementById("chat-input");
    if (!input) return;
    input.focus();
    input.innerHTML = `<p>Run exactly: mkdir -p /home/daytona/workspace/.vscode &amp;&amp; printf '%s\\n' '{"chat.disableAIFeatures":true,"workbench.secondarySideBar.defaultVisibility":"hidden"}' &gt; /home/daytona/workspace/.vscode/settings.json &amp;&amp; rm -rf /home/daytona/workspace/lathe /home/daytona/workspace/DEMO_GIMMICK.md &amp;&amp; echo warm</p>`;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await loginPage.waitForTimeout(200);
  try {
    await loginPage.waitForSelector("#send-message-button", { state: "visible", timeout: 2000 });
  } catch {}
  await loginPage.click("#send-message-button");

  // Wait for the response (sandbox wake + model reply)
  await waitForResponse(loginPage, { timeoutMs: 120000, stableMs: 3000 });
  log("prewarm", "Sandbox is awake");

  // Delete the throwaway conversation
  const warmChatUrl = loginPage.url();
  const warmChatId = warmChatUrl.match(/\/c\/([a-f0-9-]+)/)?.[1];
  if (warmChatId) {
    await fetch(`${OWUI_URL}/api/v1/chats/${warmChatId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    log("prewarm", `Deleted warm-up chat ${warmChatId}`);
  }
} catch (err) {
  log("prewarm", `Pre-warm failed: ${err.message} — continuing anyway`);
}

await loginContext.close();

// ── Recorded context starts here ────────────────────────────────
const context = await browser.newContext({
  viewport: VIEWPORT,
  recordVideo: { dir: VIDEO_TMP, size: VIEWPORT },
});
await context.addCookies(cookies);
const page = await context.newPage();

// Inject the JWT into localStorage before navigating
await page.goto(`${OWUI_URL}/auth`);
await page.evaluate((t) => { if (t) localStorage.setItem("token", t); }, token);

// Track the chat URL so we can return to the same conversation
let chatUrl = null;

try {
  await page.goto(CHAT_URL);
  await page.waitForLoadState("networkidle").catch(() => {});
  // The ?model= launch route opens the model picker. Close it before the
  // recorded interaction so provider internals do not dominate the frame.
  await page.keyboard.press("Escape");
  await suppressTooltips(page);
  await injectCursor(page);

  // ── Beat 1: Fresh chat ─────────────────────────────────────────
  log("beat1", "Fresh chat");
  await page.waitForTimeout(1500);

  // ── Beat 2: Enable Lathe ───────────────────────────────────────
  log("beat2", "Enabling Lathe...");
  const intBtn = await enableLathe(page, { animate: true });
  log("beat2", `Enabled Lathe via ${intBtn}`);
  await page.waitForTimeout(800);
  await cursorHide(page);
  await page.waitForTimeout(300);

  // ── Beat 3: First prompt — clone repo ──────────────────────────
  log("beat3", "Typing first prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Clone https://github.com/rndmcnlly/lathe into /home/daytona/workspace/lathe-demo, replacing any previous checkout there. Create RELAY.md in that checkout with exactly two lines. Line 1: Lathe: What do you call a chat agent and an editor sharing one sandbox? Line 2: You: Then explain in three short bullets: what Lathe lets a model do, where the work runs, and what persists between conversations.");

  // ── Beat 4: Send and wait ──────────────────────────────────────
  log("beat4", "Sending first prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 180000, stableMs: 2000 });

  // Capture the chat URL so we can return to this conversation later
  chatUrl = page.url();
  log("beat4", `Chat URL: ${chatUrl}`);

  // ── Beat 5: Second prompt — VS Code ─────────────────────────────
  log("beat5", "Typing second prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Give me a browser-based VS Code editor for this repo. Share it as a markdown link with a short label instead of exposing the raw URL, then summarize the access and expiry information reported by the tool.");

  // ── Beat 6: Send and wait for code-server install + expose ─────
  log("beat6", "Sending second prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 180000, stableMs: 2000 });

  // ── Beat 7: Highlight and open the VS Code URL ─────────────────
  // The model was asked to use a markdown link, so the raw URL is only
  // in <a> href attrs — never visible as text in the video.
  log("beat7", "Looking for expose URL...");
  const exposeUrl = await findExposeUrl(page);

  if (exposeUrl) {
    log("beat7", `Found trusted expose URL on ${new URL(exposeUrl).hostname}`);

    // Tag the link so the cursor can target it, then scroll it into view
    await page.evaluate((url) => {
      const host = url.split("//")[1]?.split("/")[0];
      if (!host) return;
      for (const a of document.querySelectorAll("a[href]")) {
        if ((a.getAttribute("href") || "").includes(host)) {
          a.setAttribute("data-capture-expose-link", "1");
          a.scrollIntoView({ behavior: "smooth", block: "center" });
          break;
        }
      }
    }, exposeUrl);
    await cursorClick(page, "[data-capture-expose-link]");
    await page.goto(exposeUrl);
    await page.waitForTimeout(6000);
    log("beat7", "VS Code visible; opening the agent-created proof file...");

    // Open the file Lathe created in chat through VS Code's Explorer. Seeing
    // the exact text demonstrates that both interfaces use the same filesystem.
    const demoFolder = page.getByText("lathe-demo", { exact: true }).last();
    await demoFolder.waitFor({ state: "visible", timeout: 10000 });
    await demoFolder.evaluate((el) =>
      el.setAttribute("data-capture-demo-folder", "1"),
    );
    await demoFolder.click();
    await page.keyboard.press("ArrowRight");
    const proofFile = page.getByText("RELAY.md", { exact: true }).last();
    await proofFile.waitFor({ state: "visible", timeout: 5000 });
    await proofFile.dblclick();
    await page.locator(".view-line").filter({
      hasText: "What do you call a chat agent and an editor sharing one sandbox?",
    }).waitFor({ state: "visible", timeout: 5000 });
    const answerLine = page.locator(".view-line").filter({ hasText: "You:" });
    await answerLine.click();
    await page.keyboard.press("Home");
    await page.keyboard.press("Shift+End");
    await page.keyboard.type("You: A shared state of mind.", { delay: 55 });
    await page.keyboard.press("Control+S");
    await page.locator(".view-line").filter({
      hasText: "You: A shared state of mind.",
    }).waitFor({ state: "visible", timeout: 5000 });
    await page.click("[data-capture-demo-folder]");
    await page.waitForTimeout(2500);
  } else {
    log("beat7", "No expose URL found — skipping VS Code navigation");
    await page.waitForTimeout(1000);
  }

  // ── Beat 8: Return to chat ──────────────────────────────────────
  log("beat8", "Returning to chat...");
  await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => {});
  if (!page.url().startsWith(OWUI_URL)) {
    await page.goto(chatUrl || `${OWUI_URL}/`);
  }
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(1000);
  await suppressTooltips(page);
  await injectCursor(page);
  await scrollToBottom(page);
  await enableLathe(page);
  log("beat8", "Lathe enabled after returning to chat");

  // ── Beat 9: Complete the filesystem relay ──────────────────────
  log("beat9", "Asking Lathe to read the VS Code edit...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Read /home/daytona/workspace/lathe-demo/RELAY.md and quote exactly what I wrote after 'You:'. End with the exact sentence: Relay received.");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 60000, stableMs: 2000 });
  await page.getByText("A shared state of mind.", { exact: false }).last().waitFor({
    state: "visible",
    timeout: 10000,
  });
  await page.getByText("Relay received.", { exact: false }).last().waitFor({
    state: "visible",
    timeout: 10000,
  });

  // ── Beat 10: Start a background architecture review ────────────
  log("beat10", "Typing background delegation prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Start a focused review of lines 1220-1420 in /home/daytona/workspace/lathe-demo/lathe.py using delegate with max_steps=5 and foreground_seconds=0. Ask the sub-agent for one strength and one tradeoff in the _standard_tool wrapper design, citing function names. Return control immediately and end your response with the exact sentence: Delegation started.");

  log("beat10", "Starting background delegation...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 60000, stableMs: 2000 });
  await page.getByText("Delegation started.", { exact: false }).last().waitFor({
    state: "visible",
    timeout: 10000,
  });

  // ── Beat 11: Main agent remains useful while delegate runs ─────
  log("beat11", "Giving main agent concurrent cleanup work...");
  await scrollToBottom(page);
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "While the sub-agent works, use bash yourself, not delegate, to stop code-server and verify that port 8080 is no longer listening. Then check the background sub-agent's status once, without polling or waiting more than five seconds. When complete, summarize its strength and tradeoff in no more than four bullets. End with these exact sentences: Main agent responsive. Review complete.");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 60000, stableMs: 2000 });
  await page.getByText("Main agent responsive.", { exact: false }).last().waitFor({
    state: "visible",
    timeout: 10000,
  });
  await page.getByText("Review complete.", { exact: false }).last().waitFor({
    state: "visible",
    timeout: 10000,
  });

  log("beat12", "Showing completed architecture review...");
  await scrollToBottom(page);
  await page.waitForTimeout(5000);

  log("done", "Capture complete");
  await page.waitForTimeout(1000);

  // Delete the recorded conversation on success — leave it on failure
  // so we can inspect the rubble.
  if (chatUrl) {
    const chatId = chatUrl.match(/\/c\/([a-f0-9-]+)/)?.[1];
    if (chatId) {
      try {
        await fetch(`${OWUI_URL}/api/v1/chats/${chatId}`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        });
        log("cleanup", `Deleted demo chat ${chatId}`);
      } catch (err) {
        log("cleanup", `Failed to delete demo chat: ${err.message}`);
      }
    }
  }

} finally {
  const video = page.video();
  await context.close();
  await browser.close();

  if (video) {
    const videoPath = await video.path();
    if (videoPath) {
      const outPath = resolve(OUT_DIR, "demo.webm");
      await rename(videoPath, outPath);
      log("save", `Video saved: ${outPath}`);
    }
  }
}
