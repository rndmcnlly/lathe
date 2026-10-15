/** Wait for usable chat UI, not network silence in a realtime application. */
export async function typeMessage(page, text) {
  const input = page.locator("#chat-input");
  await input.waitFor({ state: "visible", timeout: 15000 });
  await input.click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  const lines = text.split("\n");
  for (let index = 0; index < lines.length; index++) {
    // Keep the editor's own state and hard breaks, not only its visible DOM.
    if (index) await page.keyboard.press("Shift+Enter");
    await page.keyboard.insertText(lines[index]);
    await page.waitForTimeout(75);
  }
}

export async function openOwuiChat(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator("#chat-input").waitFor({ state: "visible", timeout: 15000 });
  const notice = page.getByRole("button", { name: "Okay, Let's Go!", exact: true });
  if (await notice.isVisible()) {
    await notice.click({ timeout: 3000 });
    await notice.waitFor({ state: "hidden", timeout: 3000 });
  }
  await page.keyboard.press("Escape");
}
