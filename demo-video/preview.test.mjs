import assert from "node:assert/strict";
import { test } from "node:test";
import { chromium } from "playwright";
import { followDirectoryArtifact } from "./preview.mjs";

test("directory previews follow only the exact evidenced artifact on the trusted host", { timeout: 10000 }, async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const requested = [];
    await page.route("https://**/*", async (route) => {
      requested.push(route.request().url());
      await route.fulfill({ contentType: "text/html", body: "<h1>Repair café infographic</h1>" });
    });
    await page.goto("https://preview.test/");
    assert.equal(await followDirectoryArtifact(page, "/work/report.html"), false);
    for (const href of ["https://other.test/report.html", "/wrong.html", "/report.html?token=secret"]) {
      await page.setContent(`<h1>Directory listing for /</h1><a href="${href}">report.html</a>`);
      await assert.rejects(followDirectoryArtifact(page, "/work/report.html"), /trusted preview|another file/);
    }
    await page.setContent('<h1>Directory listing for /</h1><a href="other.html">other.html</a>');
    await assert.rejects(followDirectoryArtifact(page, "/work/report.html"), /no unique link/);
    await page.setContent('<h1>Directory listing for /</h1><a href="repair%20report.html">repair report.html</a>');
    assert.equal(await followDirectoryArtifact(page, "/work/repair report.html"), true);
    assert.equal(page.url(), "https://preview.test/repair%20report.html");
    assert.deepEqual(requested, ["https://preview.test/", "https://preview.test/repair%20report.html"]);
  } finally {
    await browser.close();
  }
});
