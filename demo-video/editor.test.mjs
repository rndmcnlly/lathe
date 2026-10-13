import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { replaceEditorLine } from "./editor.mjs";

for (const answer of ["", "Roommates, they split one VM and neither one ever cleans /tmp.", "An unexpectedly long joke. ".repeat(12)]) {
test(`relay edit replaces the entire second line (${answer.length} characters) and waits for persistence`, { timeout: 10000 }, async () => {
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
    const initial = `Lathe: A shared sandbox?\nYou:${answer ? ` ${answer}` : ""}\n`;
    await page.evaluate(({ initial, mac }) => {
      const input = document.querySelector("textarea");
      const tab = document.querySelector(".tab");
      input.value = initial;
      input.setSelectionRange(0, 0);
      window.saved = initial;
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
        if (event.key.toLowerCase() === "l" && (mac ? event.metaKey : event.ctrlKey)) {
          event.preventDefault();
          // Fixture implements VS Code's logical-line selection keybinding.
          const start = input.value.lastIndexOf("\n", input.selectionStart - 1) + 1;
          const end = input.value.indexOf("\n", input.selectionEnd);
          input.setSelectionRange(start, end < 0 ? input.value.length : end + 1);
        }
        if (event.key.toLowerCase() === "s" && (mac ? event.metaKey : event.ctrlKey)) {
          event.preventDefault();
          const content = input.value;
          setTimeout(() => { window.saved = content; tab.classList.remove("dirty"); }, 200);
        }
      });
    }, { initial, mac: process.platform === "darwin" });
    await replaceEditorLine(page, { path: "/workspace/RELAY.md", line: 2, before, text: "You: A shared state of mind." });
    assert.equal(await page.evaluate(() => window.saved), "Lathe: A shared sandbox?\nYou: A shared state of mind.\n");
  } finally {
    await browser.close();
  }
});
}
