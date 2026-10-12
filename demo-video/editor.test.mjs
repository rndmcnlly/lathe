import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { replaceEditorLine } from "./editor.mjs";

test("relay edit uses the platform save shortcut and waits for asynchronous persistence", { timeout: 10000 }, async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <div class="part editor">
        <div class="tab">RELAY.md</div>
        <div class="monaco-editor" data-uri="file:///workspace/RELAY.md">
          <textarea class="inputarea"></textarea><div class="view-lines"></div>
        </div>
      </div>
    `);
    const before = "Lathe: A shared sandbox?\nYou:\n";
    await page.evaluate(({ before, mac }) => {
      const input = document.querySelector("textarea");
      const tab = document.querySelector(".tab");
      input.value = before;
      input.setSelectionRange(0, 0);
      window.saved = before;
      function render() {
        const rows = input.value.split("\n").map((line) => {
          const row = document.createElement("div");
          row.className = "view-line";
          row.textContent = line;
          return row;
        });
        document.querySelector(".view-lines").replaceChildren(...rows);
      }
      render();
      input.addEventListener("input", () => { tab.classList.add("dirty"); render(); });
      input.addEventListener("keydown", (event) => {
        if (event.key.toLowerCase() === "s" && (mac ? event.metaKey : event.ctrlKey)) {
          event.preventDefault();
          const content = input.value;
          setTimeout(() => { window.saved = content; tab.classList.remove("dirty"); }, 200);
        }
      });
    }, { before, mac: process.platform === "darwin" });
    await replaceEditorLine(page, { path: "/workspace/RELAY.md", line: 2, before, text: "You: A shared state of mind." });
    assert.equal(await page.evaluate(() => window.saved), "Lathe: A shared sandbox?\nYou: A shared state of mind.\n");
  } finally {
    await browser.close();
  }
});
