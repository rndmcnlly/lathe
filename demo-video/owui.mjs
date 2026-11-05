/** Wait for usable chat UI, not network silence in a realtime application. */
export async function saveRecording(page, context, path) {
  const video = page?.video();
  await context.close();
  // Playwright copies the completed recording, including across filesystems.
  if (video) await video.saveAs(path);
}

export async function enableLathe(page, {
  click = async (target) => target.click(),
  emphasize = async () => {},
} = {}) {
  const integrations = page.getByRole("button", { name: "Integrations", exact: true });
  await click(integrations, 650);
  const tools = page.getByRole("menuitem", { name: /^Tools\s+\d+$/ });
  await tools.waitFor({ state: "visible", timeout: 5000 });
  await click(tools, 650);

  // OWUI's IntegrationsMenu exposes the row as the checkable control. The
  // visual Switch is inert, and Valves is a separate, independently named button.
  const lathe = page.getByRole("menuitemcheckbox", { name: /^Lathe(?: Test)?$/ });
  await lathe.waitFor({ state: "visible", timeout: 5000 });
  await emphasize(lathe);
  if (!await lathe.isChecked()) await click(lathe, 900);
  await page.getByRole("menuitemcheckbox", {
    name: /^Lathe(?: Test)?$/, checked: true,
  }).waitFor({ state: "visible", timeout: 5000 });
  await page.keyboard.press("Escape");
  return "Integrations";
}

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
