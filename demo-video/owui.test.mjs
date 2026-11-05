import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { enableLathe, openOwuiChat, saveRecording, typeMessage } from "./owui.mjs";

test("recording delivery copies completed video instead of requiring a same-filesystem rename", { timeout: 15000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), "lathe-recording-"));
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ recordVideo: { dir: join(directory, "raw") } });
    const page = await context.newPage();
    await page.setContent("<h1>Recorded artifact</h1>");
    await page.screenshot(); // Ensure the browser has painted a frame.
    const original = await page.video().path();
    const destination = join(directory, "demo.webm");
    await saveRecording(page, context, destination);
    const bytes = await readFile(destination);
    assert.ok(bytes.length > 0);
    assert.deepEqual(bytes, await readFile(original), "delivery copies rather than renames the recording");
  } finally {
    await browser.close();
    await rm(directory, { recursive: true, force: true });
  }
});

// Accessibility contract from OWUI v0.13.0 IntegrationsMenu.svelte:
// https://github.com/open-webui/open-webui/pull/32940 (menu roles, Oct 22)
// https://github.com/open-webui/open-webui/pull/33065 (sibling controls, Oct 24)
// Deliberately omit upstream IDs/classes and keep the visual switch inert.
function integrationsFixture(name, checked, { broken = false } = {}) {
  return `<!doctype html><html><body>
    <p>Lathe</p><button>Tools 99</button>
    <button aria-label="Integrations" onclick="document.querySelector('[role=menu]').hidden = false">Open</button>
    <div role="menu" hidden>
      <button role="menuitem" onclick="this.hidden = true; document.querySelector('#tool-list').hidden = false">Tools <span>2</span></button>
      <section id="tool-list" hidden>
        <button role="menuitemcheckbox" aria-checked="false">Other tool</button>
        <div role="presentation" onclick="if (event.target.closest('[role=menuitemcheckbox]')) {
          window.toggleClicks++;
          if (!${broken}) this.querySelector('[role=menuitemcheckbox]').setAttribute('aria-checked', 'true');
        }">
          <button role="menuitemcheckbox" aria-checked="${checked}">${name}</button>
          <button aria-label="Valves" onclick="window.valveClicks++">Settings</button>
          <div inert><button role="switch" aria-checked="false">Decorative switch</button></div>
        </div>
      </section>
    </div>
    <script>
      window.toggleClicks = 0;
      window.valveClicks = 0;
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape') document.querySelector('[role=menu]').hidden = true;
      });
    </script>
  </body></html>`;
}

for (const name of ["Lathe", "Lathe Test"]) {
  for (const checked of [false, true]) {
    test(`enable ${name} through menu accessibility semantics, initially checked=${checked}`, { timeout: 10000 }, async () => {
      const browser = await chromium.launch({ headless: true });
      try {
        const page = await browser.newPage();
        await page.setContent(integrationsFixture(name, checked));
        await enableLathe(page);
        // Reopen the menu to observe the result, not the adapter's return value.
        await page.getByRole("button", { name: "Integrations", exact: true }).click();
        assert.equal(await page.getByRole("menuitemcheckbox", { name, exact: true }).isChecked(), true);
        assert.equal(await page.getByRole("menuitemcheckbox", { name: "Other tool" }).isChecked(), false);
        assert.deepEqual(await page.evaluate(() => [window.toggleClicks, window.valveClicks]), [checked ? 0 : 1, 0]);
        assert.equal(await page.getByRole("switch").isChecked(), false, "decoration must not supply the authoritative checked state");
      } finally { await browser.close(); }
    });
  }
}

test("enabling a tool requires observed checked state, not just a successful click", { timeout: 10000 }, async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(integrationsFixture("Lathe", false, { broken: true }));
    await assert.rejects(enableLathe(page), /Timeout/);
    assert.equal(await page.evaluate(() => window.toggleClicks), 1);
    assert.equal(await page.getByRole("menu").isVisible(), true, "failure must not advance to dismissing the menu");
  } finally { await browser.close(); }
});

test("chat readiness dismisses release notes despite ongoing network activity and survives a fresh recording context", { timeout: 10000 }, async () => {
  const browser = await chromium.launch({ headless: true });
  let ongoingRequests = 0;
  const html = `<!doctype html><html><body><script>
    fetch('/ongoing');
    setTimeout(() => {
      document.body.innerHTML = '<div id="chat-input" contenteditable="true"></div>';
      if (!localStorage.getItem('release-notes-dismissed')) {
        const dialog = document.createElement('div');
        dialog.setAttribute('role', 'dialog');
        const button = document.createElement('button');
        button.textContent = "Okay, Let's Go!";
        button.onclick = () => {
          localStorage.setItem('release-notes-dismissed', 'true');
          dialog.remove();
        };
        dialog.appendChild(button);
        document.body.appendChild(dialog);
      }
    }, 100);
  </script></body></html>`;
  const setup = async (context) => {
    await context.route("http://owui.test/**", async (route) => {
      if (route.request().url().endsWith("/ongoing")) {
        ongoingRequests += 1;
        return; // Deliberately never resolve: networkidle cannot be reached.
      }
      await route.fulfill({ contentType: "text/html", body: html });
    });
    return context.newPage();
  };
  try {
    const login = await browser.newContext();
    const page = await setup(login);
    await openOwuiChat(page, "http://owui.test/");
    assert.equal(await page.getByRole("dialog").count(), 0);
    assert.equal(await page.evaluate(() => localStorage.getItem("release-notes-dismissed")), "true");

    const storageState = await login.storageState();
    await login.close();
    const recording = await browser.newContext({ storageState });
    const recordedPage = await setup(recording);
    await openOwuiChat(recordedPage, "http://owui.test/");
    assert.equal(await recordedPage.getByRole("dialog").count(), 0);
    // A later chat navigation also needs UI readiness, not network silence.
    await openOwuiChat(recordedPage, "http://owui.test/c/chat");
    assert.equal(await recordedPage.locator("#chat-input").isVisible(), true);
    assert.equal(ongoingRequests, 3);
  } finally {
    await browser.close();
  }
});

test("multiline prompts use real editor input and preserve CSV hard breaks", { timeout: 10000 }, async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<div id="chat-input" contenteditable="true"><p>Old draft</p></div>');
    const csv = "day,repaired\nMonday,8\nTuesday,12";
    await typeMessage(page, csv);
    assert.equal(await page.locator("#chat-input").innerText(), csv);
    await typeMessage(page, "A replacement");
    assert.equal(await page.locator("#chat-input").innerText(), "A replacement");
  } finally { await browser.close(); }
});
