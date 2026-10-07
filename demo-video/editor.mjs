/** Edit the small relay document through the file editor's keyboard surface. */
export async function replaceEditorLine(page, { path, line, text, before }, timeoutMs = 5000) {
  const lines = before.split("\n");
  if (!Number.isInteger(line) || line < 1 || line > lines.length || /[\r\n]/.test(text)) {
    throw new Error("Expected one existing relay line and a single-line replacement");
  }
  // Chat inputs also use Monaco. Match the file URI inside the editor area,
  // and let strict locators reject ambiguity rather than choosing .last().
  // code-server uses vscode-remote://<host>/<path>, standalone Monaco file://.
  const editor = page.locator(`.part.editor .monaco-editor[data-uri$=${JSON.stringify(path)}]`);
  const input = editor.locator("textarea.inputarea, .native-edit-context");
  const rendered = editor.locator(".view-lines");
  async function verify(content) {
    const expected = content.replace(/\n/g, "");
    const deadline = Date.now() + timeoutMs;
    let actual;
    do {
      // Monaco renders spaces as NBSP and soft wraps as separate view rows.
      actual = await rendered.evaluate((element) => [...element.querySelectorAll(".view-line")]
        .map((row) => row.textContent.replace(/\u00a0/g, " ")).join(""));
      if (actual === expected) return;
      await page.waitForTimeout(50);
    } while (Date.now() < deadline);
    throw new Error(`Editor content mismatch for ${path}: expected ${JSON.stringify(expected)}, saw ${JSON.stringify(actual)}`);
  }
  await input.waitFor({ state: "visible", timeout: timeoutMs });
  await verify(before);
  await input.focus();
  await input.press("Control+Home");
  // Walk characters, not visual rows: the first relay line may soft-wrap.
  const prefix = lines.slice(0, line - 1).join("\n") + (line > 1 ? "\n" : "");
  for (let index = 0; index < prefix.length; index++) await input.press("ArrowRight");
  for (let index = 0; index < lines[line - 1].length; index++) await input.press("Shift+ArrowRight");
  await input.pressSequentially(text, { delay: 55 });
  lines[line - 1] = text;
  await verify(lines.join("\n"));
  await input.press("Control+S");
  // The subsequent Lathe read independently checks that this reached disk.
  return { observations: [`editor.replaced-line=${line}`, "editor.content=verified"] };
}
