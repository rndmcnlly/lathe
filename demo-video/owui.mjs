/** Wait for usable chat UI, not network silence in a realtime application. */
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
