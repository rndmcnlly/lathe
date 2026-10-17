import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";
import { openOwuiChat, typeMessage } from "./owui.mjs";

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
