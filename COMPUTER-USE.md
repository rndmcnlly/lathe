# Shared desktop computer use

Implemented and deployed to the existing `lathe` toolkit on
`https://chat.adamsmith.as` with Adam's approval, 2026-10-24. The runtime remains
in `lathe.py`; the preview wrapper is unchanged. Agent-facing operation schemas
and workflow guidance live in `lathe(manpage="computer")`.

## Contract

- `peek(command, args)` observes status, process diagnostics, display/windows,
  pointer position, screenshots and accessibility state. Accessibility search
  is a read-only POST, so classification is by effects rather than HTTP method.
- `poke(command, args)` starts/stops desktop processes, restarts one process,
  sends mouse/keyboard input or invokes accessibility actions. Recording is not
  part of the initial surface.
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
- No input is automatically retried. Success acknowledges delivery, not the
  intended application's outcome. Errors omit response bodies and typed text.
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

Default Daytona snapshots provide desktop dependencies. Browser installation,
profiles and application startup remain ordinary sandbox program work. Custom
snapshots may require administrator provisioning. There is no embedded OWUI
desktop or new cross-site iframe authentication protocol.

## Qualification

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
