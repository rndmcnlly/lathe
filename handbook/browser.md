# Shared browser: Playwright, PDFs and waiting

Lathe's `page.*` commands cover most browsing: open, read text, click, type,
save and evaluate. Read the internal computer manual first
(`lathe(manpage="computer")`). Use this page when a task needs more than one
command at a time: scripted multi-step flows, explicit waits, downloads, or
many pages in a row.

## The shared browser

Any `poke` page command launches Chromium visibly on the shared desktop, with
the profile `~/.config/lathe-browser` and DevTools on `127.0.0.1:9222`. Attach
to that instance rather than launching a second, headless one: the user sees
it over VNC, and it holds their logins.

DevTools on port 9222 is full control of a logged-in browser. Never `expose`
it, tunnel it, or bind it beyond loopback. Do not disable Chromium's sandbox.

Subagents and other chats share this browser and its default tab (the most
recently used). Parallel workers should each `page.open` with `newTab` and pass
that `tab` to later commands, or work one at a time. Playwright scripts should
open their own page (`context.new_page()`) rather than drive `context.pages[0]`
while another agent may be using it.

If no browser answers on 9222, call `poke(command="page.open", ...)` first.
If a Chromium already uses the Lathe profile without remote debugging (for
example one launched by hand), ask the user before closing it.

## Playwright over CDP

Install into the user's Python (about 8 s on a default image). The browser
binary is already present, so no `playwright install` download is needed:

```sh
python3 -m pip install --user playwright
```

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]          # the shared, logged-in context
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://example.org", wait_until="domcontentloaded")
    page.get_by_role("link", name="Learn more").click()
    page.wait_for_load_state("networkidle")
    print(page.url, page.title())
    # Do not call browser.close(): that would close the user's browser.
```

Prefer role and text locators (`get_by_role`, `get_by_text`) over coordinates.
Playwright's auto-waiting covers most timing; add `page.wait_for_selector(...)`
or `wait_for_url(...)` for single-page apps instead of fixed sleeps.

## PDFs and downloads

- A direct PDF link: `poke(command="page.save", args={"path": "/abs/out.pdf",
  "url": "https://..."})` fetches it with the browser's cookies. If the result
  is HTML, it is probably a login, paywall or verification page: hand that to
  the user (`expose(target="vnc", access="private")`) rather than fighting it.
- A rendered page as a document: `page.save` with `"format": "pdf"`.
- Downloads triggered by clicks: with Playwright,

  ```python
  with page.expect_download() as info:
      page.get_by_text("Download").click()
  info.value.save_as("/home/daytona/workspace/file.pdf")
  ```

  Without Playwright, Chromium saves to `~/Downloads` by default, or opens a
  native Save dialog. In native dialogs, click the Save button with
  `poke(command="mouse.click", ...)`; pressing Enter may do nothing.

Verify saved files independently (`file`, `head -c 8`, `read`, or `view` for
images) before reporting success.

## Human verification

CAPTCHAs, MFA prompts and login pages are the user's to complete. Give them
the private VNC link from `expose`, pause all input and screenshots until they
say ready, then observe again. Never compose a link for a human yourself.
