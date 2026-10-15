# Interactive Docker design trials

Use this loop to develop Lathe's tool experience and handbook with a human
actually using Open WebUI. Keep the installation available between experiments.
The disposable automated monitor in `CI-MONITOR.md` serves a different purpose.

## 1. Establish an isolated installation

- Run local Docker OWUI, bound to loopback, with a dedicated trial user,
  toolkit ID, workspace model, and unique Daytona deployment label.
- Use an explicitly selected model and authorized inference credentials. Record
  provider privacy settings and bound experiment cost and tool iterations.
- Keep production OWUI and production toolkit identities out of the setup.
  Do not run competing live suites against the trial's identities.
- Give cloud sandboxes explicit idle-stop and hard-expiry limits; provision no
  extra volumes. Record the deadline in UTC and the user's local timezone.
- Keep credentials in a protected, gitignored file. Give the user the app URL,
  selected model, and login details through the private handover.

Create a gitignored run directory under `monitor-artifacts/` with an
`OPERATIONS.md`: exact container names, ports, toolkit/model IDs, sandbox label
and ID, deadlines, credential-file location, and restart/cleanup commands.
Record actual commands used so another session can resume without guessing.

## 2. Stage the working source

Run the required offline checks, then update only the trial toolkit from the
working `lathe.py`. Preserve valves, model attachment, chats, application files,
and user-grown skills. Fetch the installed source back and compare SHA-256;
verify the loaded tool schema and new valve fields. Refresh OWUI's model cache
when capability changes require it.

Record the commit, uncommitted source hash, OWUI version/image, and the scope of
qualification. A successful source upload is not a successful agent journey.

## 3. Serve the development handbook

Agents read `handbook/`, not the public landing page in `docs/`. Serve the full
current directory with ordinary HTTP, including linked guides. Do not default
to the public branch while testing unpublished edits.

The simplest route when local Docker is unreachable from Daytona is to copy
the handbook into a trial-owned directory inside Daytona and serve it there:

```sh
python3 -m http.server 8498 --bind 127.0.0.1 --directory /ABS/TRIAL/handbook
```

Launch it using the environment's normal background-process mechanism and
record its identity, logs, and restart procedure. Set the trial's `handbook_url`
to `http://127.0.0.1:8498/README.md`. This loopback address is meaningful inside
Daytona, not inside OWUI or the user's browser. An approved reachable development
server is another option; no GitHub imitation is needed.

Fetch the index and linked guides from inside the sandbox and compare their
bytes with the working checkout. After edits, refresh the served copy and repeat
that check. Lathe only displays the URL; the agent chooses how to fetch it.

**Leave the server and valve configured throughout the interactive trial.**
Restoring an empty valve immediately after a scripted check silently removes the
experience the user is trying to evaluate. Record the retained override and its
original value for eventual cleanup. Sandbox sleep stops processes, so supply a
simple restart command in the handover and, when useful, trial-only
`onboarding_instructions`. A startup helper must identify its own listener before
reusing or stopping it.

## 4. Test the learning loop

Use distinct fresh conversations, retaining the sandbox filesystem:

1. **Reference access:** ask the agent to find the configured handbook through
   Lathe's manual, fetch the index and an app guide, and report a specific detail.
   Inspect tool evidence; plausible answers from model knowledge are insufficient.
2. **First use:** request an app such as code-server. Observe handbook access,
   installation, readiness checks, private exposure, and an offer to save a skill.
   Approve skill creation and independently read the saved file.
3. **Fresh-chat reuse:** ask again without supplying the recipe or prior chat.
   Verify discovery and reading of the skill, appropriate process reuse, and a
   restart after a controlled stop. Record time and tool calls, not a fixed speed
   threshold. Ensure no unnecessary reinstall occurs.
4. **Port negotiation:** occupy a test port with a separately identified listener.
   Verify the agent inspects ownership, preserves unrelated work, and negotiates
   reuse or another port instead of blindly killing a PID.
5. **Unavailable references:** check that empty or unreachable handbook URLs leave
   local-resource work useful and that the agent honestly reports failed access.

Lathe-specific skills live in `~/.local/share/lathe/skills/`; shared and project
skills may instead be appropriate. Preserve the user's adapted skills. To test
first use again, use an untouched app or an explicitly agreed isolated fixture;
do not delete user knowledge to obtain a clean result.

Independently inspect files, process identity, HTTP responses, and browser
behavior. For an IDE, a working page alone is insufficient: check login when
configured and terminal execution over WebSocket, then read the resulting file
independently. Record OOMs and recovery separately from successful runs.

## 5. Qualify preview TLS honestly

OWUI's HTTP client, sandbox clients, and the user's browser have separate trust
contexts. A certificate trusted inside Docker can still fail in the browser.
`curl -k`, `verify=False`, or clicking through a warning is not TLS qualification.

Prefer an already trusted test route. If temporary local CA trust is necessary,
obtain explicit user permission first. Limit it to the login keychain; record
the exact fingerprint and prior trust state. Use a bounded cleanup mechanism
that removes only newly added trust/certificates on success or failure. Verify
removal and that default verification rejects the trial certificate again.
Never remove preexisting user trust or silently change the system keychain.

Remove temporary trust immediately after the browser check, even if the app
trial continues. Explain that retained preview links will again be untrusted.

## 6. Handover, iterate, and close

Leave a concise current status in `OPERATIONS.md`: app/preview URLs, source and
handbook hashes, skill paths, checks performed, inference cost, known limits,
idle restart commands, and hard expiry. A fresh chat receives new orientation;
an existing chat can explicitly call `lathe(manpage="services")`.

Keep source redeployment, handbook refresh, and skill adaptation distinct. A
handbook edit needs no toolkit redeploy; an existing user skill may intentionally
take precedence over the new recipe. During iteration, investigate that behavior
instead of assuming the agent read the handbook.

At the agreed end of the trial, stop owned fixture servers/tunnels, restore owned
valve overrides without overwriting intervening user edits, and verify temporary
trust is absent. Retain or remove Docker data according to the user's choice;
cloud cleanup must target the exact run label/identity. Never use broad cleanup
commands against a shared Daytona account.

## Reference run

The initial handbook/IDE trial's local record is
`monitor-artifacts/local-lean-20261014-b/OPERATIONS.md`, with detailed evidence in
`ide-handbook-20261015/qualification-report.md` beneath that directory. Those are
local historical artifacts, not reusable infrastructure addresses or credentials.
