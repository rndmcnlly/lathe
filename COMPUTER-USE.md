# Shared desktop computer use

Implemented and deployed to the existing `lathe` toolkit on
`https://chat.adamsmith.as` with Adam's approval, 2026-10-24. The runtime remains
in `lathe.py`; the preview wrapper is unchanged. Agent-facing operation schemas
and workflow guidance live in `lathe(manpage="computer")`.

## Contract

- `peek(command, args)` observes status, process diagnostics, display/windows,
  pointer position, screenshots, accessibility state and the shared browser
  (`page.list`, `page.text`). Accessibility search is a read-only POST, so
  classification is by effects rather than HTTP method. peek never starts the
  desktop or browser, and refuses display queries while the display is down.
- `poke(command, args)` starts/stops desktop processes, restarts one process,
  sends mouse/keyboard input, invokes accessibility actions or drives the shared
  browser (`page.open/click/type/save/eval`). Any non-lifecycle poke first starts
  a down desktop; page pokes also launch Chromium. Recording is not part of the
  surface.
- "Down" means the `xvfb` process is not running, read from per-process status.
  Daytona's aggregate status reports `partial` after sandbox wake even when
  every display process is down. Live evidence (2026-10-27): mouse/keyboard
  input or `mouse.position` on a down display kills Daytona's computer-use
  plugin ("connection is shut down" on every later call) until the sandbox
  restarts; screenshots merely fail. Hence auto-start before input and the
  peek guard.
- Browser commands run one embedded, stateless sandbox script per call (stdlib
  plus the image's preinstalled `websockets`), talking to Chromium DevTools on
  sandbox loopback 127.0.0.1:9222. No CDP traffic from OWUI, no preview of 9222.
  Chromium runs visibly with its sandbox intact and a persistent profile at
  `~/.config/lathe-browser`. Clicks are real `Input.dispatchMouseEvent` events
  at the element center. `page.click` prefers exact clickable text, then an
  exact descendant label's nearest clickable ancestor, then a unique substring.
  Ambiguous text or selectors, covered centers, and centers outside the viewport
  are refused before input; inspection/scrolling is not an atomic transaction
  with input on a shared page. `page.save` with `url` fetches through the page with
  its cookies, falling back to `Network.loadNetworkResource` for cross-origin.
- A closed command registry owns transport mapping and strict argument models.
  Unknown commands/fields, invalid types (including bool-for-int), invalid
  container members and bounds fail before sandbox I/O. The argument reference
  is generated from those models, avoiding a second schema to keep synchronized.
- Desktop calls use the authenticated Daytona Toolbox connection, not BayLeaf's
  owner API or localhost. No platform credentials are placed in the sandbox.
  The ordinary sandbox lifecycle still applies; observation never implicitly
  starts or repairs desktop processes.
- Responses are bounded. Images accept raw bytes or base64 JSON with
  `screenshot`/`Screenshot`, sniff actual content and preserve decoded bytes.
  Images are limited to 4 MiB and returned without prepended harness notices.
  Known non-vision models are refused before I/O for image commands only.
  Screenshots are never scaled (no `scale` argument): image pixels are input
  coordinates, except that region images are offset by the region origin.
- No input is automatically retried. Success acknowledges delivery, not the
  intended application's outcome. Errors carry only Daytona's JSON `message`
  (or the browser script's message), bounded to 300 characters, with any
  8-character run shared with typed fields (`text`, `value`, `js`, `url`,
  `name`) masked; short typed values that appear cause omission.
- `expose(target="vnc", access=...)` requires active desktop status, resolves
  Daytona's built-in port 6080 and returns `/vnc.html` with autoconnect/scaling
  and the `websockify` path. Direct-preview query credentials are preserved.
  It uses the ordinary preview registration/policy, including upstream headers.
  It never starts the desktop and never downgrades failed private requests.
- Private VNC is strong advice, not a target-specific harness restriction.
  Explicit public access remains available, including direct bearer fallback.

## Coordination and limits

The human and all agents/chats share the same desktop. Human login requires an
explicit handoff: stop input and screenshots until the user says ready, then
observe afresh. No enforced floor control, generation handle or atomic
observe-and-act transaction is claimed. Other sandbox programs can bypass this
interface. Logging in grants sandbox software access to that browser session;
screenshots sent to OWUI become conversation history. Preview expiry is not
logout. Stopping the stack does not promise to reap detached session daemons.

Default Daytona snapshots provide desktop dependencies, Chromium and Python
`websockets`. Other browsers, profiles and application startup remain ordinary
sandbox program work. Custom
snapshots may require administrator provisioning. There is no embedded OWUI
desktop or new cross-site iframe authentication protocol.

## Qualification

Browser verbs and round-4 AX revisions (2026-10-27, branch `browser-page-verbs`):

- Offline suite: 693 tests passed (the page-script tests run the shipped
  JavaScript in Node and skip without it).
- Daytona integration: 11/11 phases passed, including browser `page.*` commands
  with desktop auto-start.
- Isolated OWUI deployment (`lathe_test`): 6/6 scenarios passed on the second
  run. The first run failed bash dispatch because the model answered without
  calling the tool (nondeterministic smoke test, not a Lathe fault).
- Local Docker AX trial with GLM-5.3 Flash, four rounds over a fixture archive,
  parallel subagents and a real Cloudflare-gated ACM paper: all round-4
  deliverables correct, with fewer calls than round 3 (fixture 27 to 18, ACM 32
  to 18 with 8 error results down to 0). Cloudflare's current interstitial is
  detected and the authorized one-click recipe was followed and reported
  honestly. Residual: agents rarely offer the VNC live view unprompted or ask
  consent before first browser use; handoffs often omit the idle window.

Earlier shared-desktop release (v1.2.0):

- Offline suite: 581 tests passed, including command segregation and transport,
  pre-I/O validation, image formats/decoding paths, deferred image notices,
  vision gating, bounds, redacted failures/no retries, VNC landing paths,
  public fallback, and private failure behavior. Existing loader tests cover
  normal import and future-annotations execution.
- Daytona integration: 10/10 phases passed on an isolated test identity.
  Computer-use evidence includes start/status, display/windows/pointer reads,
  four full/region/compressed screenshot paths, real keyboard input consumed by
  an Xfce terminal and independently verified through a file read, accessibility
  tree retrieval, direct noVNC landing-page retrieval and an HTTP 101 WebSocket
  upgrade. The disposable desktop had no authenticated browser session.
- Isolated OWUI deployment: 6/6 scenarios passed, including the complete schema,
  real-model `peek`/`poke` dispatch and native screenshot lifting (no base64 in
  the text result). Both live suites completed their cleanup.
- Production deployment: source read back byte-identical, `peek`/`poke` present
  in OWUI specs, valves and access grants unchanged. The previous source matched
  repository HEAD. Its source, metadata and valves were backed up outside the
  checkout under the session's approved temporary directory before updating.

Remaining live gates: private noVNC browser sign-in/WebSocket interaction through
the existing wrapper, human login/resume experience, and the full input and
accessibility mutation matrix (mouse/drag/held keys/node actions). Synthetic
transport tests are not functional qualification of those upstream operations.
Production deployment did not start, inspect or manipulate Adam's desktop.

Upstream references inspected: Daytona's current computer-use documentation,
BayLeaf `api/DESKTOP.md` and `api/COMPUTER-USE.md`. BayLeaf's desktop and owner
API are useful precedents, not Lathe runtime dependencies.
