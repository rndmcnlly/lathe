#!/usr/bin/env node
/**
 * Playwright video capture of a real Lathe session on an Open WebUI instance.
 *
 * Captures: login → enable Lathe → clone a repo → ask for VS Code →
 * model calls expose(target="code-server") → open the live IDE URL →
 * return to chat → ask model to stop the server.
 *
 * Usage:  node capture.mjs
 * Env:    DEMO_OWUI_URL, DEMO_EMAIL, DEMO_PASS (loaded from .env if present)
 * Output: capture.webm
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
const EMAIL = process.env.DEMO_EMAIL;
const PASS = process.env.DEMO_PASS;
if (!OWUI_URL || !EMAIL || !PASS) { console.error("Set DEMO_OWUI_URL, DEMO_EMAIL, and DEMO_PASS"); process.exit(1); }

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
    ].join("");
    document.head.appendChild(s);
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
  return await page.evaluate(() => {
    const PROXY_RE = /https:\/\/\d+-[\w.-]+(?:daytonaproxy|proxy\.app\.daytona)[^\s)"']*/;
    for (const a of document.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href") || "";
      if (PROXY_RE.test(href)) return href;
    }
    return null;
  });
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
  if (!generationStarted) return;

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
const loginPage = await loginContext.newPage();
await loginPage.goto(`${OWUI_URL}/auth`);
await loginPage.waitForLoadState("networkidle").catch(() => {});
await loginPage.waitForTimeout(500);

if (loginPage.url().includes("/auth")) {
  log("login", "Signing in...");
  const emailInput = 'input[placeholder="Enter Your Email"]';
  const passInput = 'input[placeholder="Enter Your Password"]';
  await loginPage.fill(emailInput, EMAIL);
  await loginPage.fill(passInput, PASS);
  await loginPage.click('button[type="submit"]');
  await loginPage.waitForTimeout(500);

  // Dismiss "What's New" modal if present
  await loginPage.evaluate(() => {
    for (const b of document.querySelectorAll("button"))
      if (b.textContent.trim() === "Okay, Let's Go!") b.click();
  });
  await loginPage.waitForTimeout(500);
  log("login", "Logged in");
} else {
  log("login", "Already logged in (cookies persisted)");
}

// Extract auth token from localStorage (OWUI stores JWT there, not in cookies)
const token = await loginPage.evaluate(() => localStorage.getItem("token"));
const cookies = await loginContext.cookies();
log("login", `Token: ${token ? token.slice(0, 20) + "..." : "null"}`);
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
  await page.goto(`${OWUI_URL}/`);
  await page.waitForLoadState("networkidle").catch(() => {});
  await suppressTooltips(page);
  await injectCursor(page);

  // ── Beat 1: Fresh chat ─────────────────────────────────────────
  log("beat1", "Fresh chat");
  await page.waitForTimeout(1500);

  // ── Beat 2: Enable Lathe ───────────────────────────────────────
  log("beat2", "Enabling Lathe...");
  // The integrations button ID varies across OWUI versions
  const intBtn = await page.evaluate(() => {
    const candidates = ["#integration-menu-button", "#tools-menu-button"];
    for (const sel of candidates) {
      if (document.querySelector(sel)) return sel;
    }
    // Fallback: find by aria-label or nearby text
    for (const b of document.querySelectorAll("button")) {
      const label = (b.getAttribute("aria-label") || "").toLowerCase();
      if (label.includes("tool") || label.includes("integration")) return `#${b.id}`;
    }
    return null;
  });
  log("beat2", `Integration button: ${intBtn}`);
  if (!intBtn) {
    // Dump available button IDs for debugging
    const ids = await page.evaluate(() =>
      [...document.querySelectorAll("button[id]")].map(b => `${b.id}: ${b.textContent.trim().slice(0, 30)}`).join(", ")
    );
    log("beat2", `Available buttons: ${ids}`);
    throw new Error("Could not find integration menu button");
  }
  await cursorClick(page, intBtn);
  await page.click(intBtn);
  await page.waitForTimeout(500);

  // Click "Tools NN" row
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("*")) {
      if (el.textContent.trim().startsWith("Tools ")) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height < 60 && r.height > 20 && r.y > 200) {
          el.click(); return;
        }
      }
    }
  });
  await page.waitForTimeout(500);

  // Highlight the Lathe row, then toggle it on with animated cursor
  await page.evaluate(() => {
    for (const b of document.querySelectorAll("button")) {
      if (b.textContent.trim().endsWith("Lathe") && b.getBoundingClientRect().y > 0) {
        b.setAttribute("data-capture-lathe-row", "1");
        break;
      }
    }
  });
  // The toggle is a button[role="switch"] inside the Lathe row.
  // Clicking the row itself triggers the toggle logic in OWUI.
  await cursorClick(page, "[data-capture-lathe-row]");
  await page.click("[data-capture-lathe-row]");
  const toggled = await page.evaluate(() => {
    const row = document.querySelector("[data-capture-lathe-row]");
    if (!row) return false;
    const sw = row.querySelector('button[role="switch"]');
    return sw ? sw.getAttribute("aria-checked") === "true" : false;
  });
  log("beat2", `Toggled: ${toggled}`);
  await page.waitForTimeout(800);
  await page.keyboard.press("Escape");
  await cursorHide(page);
  await page.waitForTimeout(1000);

  // ── Beat 3: First prompt — clone repo ──────────────────────────
  log("beat3", "Typing first prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Clone https://github.com/rndmcnlly/lathe and give me a friendly one-paragraph description of what it does.");
  await page.waitForTimeout(500);

  // ── Beat 4: Send and wait ──────────────────────────────────────
  log("beat4", "Sending first prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 180000, stableMs: 5000 });

  // Capture the chat URL so we can return to this conversation later
  chatUrl = page.url();
  log("beat4", `Chat URL: ${chatUrl}`);
  await page.waitForTimeout(500);

  // ── Beat 5: Second prompt — VS Code ─────────────────────────────
  log("beat5", "Typing second prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, `Give me a VS Code editor for this repo. When you share the link, use a markdown link with a friendly label instead of showing the raw URL.`);
  await page.waitForTimeout(500);

  // ── Beat 6: Send and wait for code-server install + expose ─────
  log("beat6", "Sending second prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 180000, stableMs: 5000 });
  await page.waitForTimeout(500);

  // ── Beat 7: Highlight and open the VS Code URL ─────────────────
  // The model was asked to use a markdown link, so the raw URL is only
  // in <a> href attrs — never visible as text in the video.
  log("beat7", "Looking for expose URL...");
  const exposeUrl = await findExposeUrl(page);

  if (exposeUrl) {
    log("beat7", `Found URL: ${exposeUrl}`);

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
    await page.waitForTimeout(800);
    await cursorClick(page, "[data-capture-expose-link]");
    await page.waitForTimeout(500);

    await page.goto(exposeUrl);
    await page.waitForTimeout(6000);
    log("beat7", "VS Code visible");
  } else {
    log("beat7", "No expose URL found — skipping VS Code navigation");
    await page.waitForTimeout(1000);
  }

  // ── Beat 8: Return to chat ──────────────────────────────────────
  log("beat8", "Returning to chat...");
  await page.goto(chatUrl || `${OWUI_URL}/`);
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(1000);
  await suppressTooltips(page);
  await injectCursor(page);
  await scrollToBottom(page);

  // ── Beat 9: Ask model to stop the server ───────────────────────
  log("beat9", "Typing stop-server prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Thanks! Please stop the code-server now, I'm done with it.");
  await page.waitForTimeout(500);

  log("beat9", "Sending stop-server prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 60000, stableMs: 5000 });
  await page.waitForTimeout(500);

  // ── Beat 10: Let the cleanup response sit visibly ──────────────
  log("beat10", "Showing cleanup response...");
  await scrollToBottom(page);
  await page.waitForTimeout(500);

  // ── Beat 11: Gimmick — run whatever's in DEMO_GIMMICK.md ──────
  log("beat11", "Typing gimmick prompt...");
  await cursorClick(page, "#chat-input");
  await typeMessage(page, "Do the thing in /home/daytona/workspace/DEMO_GIMMICK.md");
  await page.waitForTimeout(500);

  log("beat11", "Sending gimmick prompt...");
  await cursorClick(page, "#send-message-button");
  await sendMessage(page);
  await cursorHide(page);
  await waitForResponse(page, { timeoutMs: 120000, stableMs: 5000 });
  await page.waitForTimeout(500);

  log("beat12", "Showing gimmick response...");
  await scrollToBottom(page);
  await page.waitForTimeout(500);

  log("done", "Capture complete");
  await page.waitForTimeout(1000);

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
