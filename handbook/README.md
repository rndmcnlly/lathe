# Agent handbook: ordinary sandbox applications

This is a mutable reference, not a skill pack, installer, or required dependency
of Lathe. Read the internal services manual and existing relevant skills first.
Consult these pages when app-specific knowledge is needed, then adapt to the
installed version and the user's preferences. Do not fetch every page on startup.

## Pages and navigation

- [code-server: browser IDE](code-server.md)
- [dufs: browser file transfer](dufs.md)
- [ttyd: browser terminal](ttyd.md)
- [Static site preview](static-site.md)
- [Shared browser: Playwright, PDFs and waiting](browser.md)

These are relative Markdown links. When reading over HTTP, resolve them against
the URL of this page, not the shell's working directory. For example, an index
at `http://reference.example/handbook/README.md` links to
`http://reference.example/handbook/code-server.md`. Retrieve that URL with an
available HTTP client, such as `curl -fsSL URL` or Python's
`urllib.request.urlopen(URL, timeout=30)`. Lathe displays the administrator's
configured URL but does not fetch it. A local test HTTP server can serve these
same files; neither GitHub nor a GitHub-shaped service is required to read them.

## First installation, not automatic upgrades

1. Inspect `uname -m`, `/etc/os-release`, installed commands, user configuration,
   and relevant skills. Examples here target blank **x86_64 Linux** with Python 3.
   Do not replace an existing installation merely because a newer release exists.
2. Check the intended root, current listeners, and process identity **before**
   installing or launching. Ask about version, authentication, extension, and
   upgrade preferences when they affect existing work.
3. Prefer an approved reachable source, including an intranet mirror or an
   existing package cache. Public GitHub metadata, release assets, and redirected
   downloads are separate network paths: any may be blocked. Report a blocked
   dependency rather than repeatedly retrying or pretending an app is bundled.
4. For a first public install, discover one stable release through the upstream
   GitHub API, inspect its tag and asset list, and select the actual compatible
   asset. Never construct an old hardcoded version URL from memory. Verify the
   publisher's SHA-256 when available (API `digest: sha256:...`, or the release's
   checksum asset). A locally computed hash alone is a receipt, not authentication.
   If no published digest exists, explicitly report the weaker HTTPS-only
   provenance and agree on an acceptable source instead of claiming verification.
5. Inspect archives before extracting into a fresh, user-owned installation
   directory. Reject absolute paths, traversal, and escaping links; code-server
   legitimately contains internal links, so use Python's `tarfile` data filter
   when available. Do not extract over an existing installation. Keep the release
   metadata, selected asset URL, and verified digest beside the installation.
6. Run the installed binary's `--version` and `--help`. Treat upstream help as
   authoritative when a flag or runtime requirement differs from these examples.
   Put binaries, app data, logs, and runtime records outside skill directories.

The download snippets below are **first-install examples**, not launchers or
ongoing update policy. Inspect them before use. Do not rerun them on every chat.

## Listener and process identity: all apps

Before launch, inspect `ss -ltnp` or `lsof -nP -iTCP:PORT -sTCP:LISTEN` if
available. Without either tool on Linux, inspect `/proc/net/tcp` and `tcp6` for
the port in hexadecimal and listener state `0A`, then map its socket inode to
`/proc/<pid>/fd` links. Permission-limited or ambiguous inspection is not proof
that a port is free or safe to take over. A Python TCP connect or bind probe can
detect occupancy but does not identify its owner.

For a candidate PID, inspect `/proc/PID/cmdline`, `/proc/PID/exe`,
`/proc/PID/cwd`, and `/proc/PID/stat`. Record executable, arguments, chosen root,
port, and the process start-time field (field 22, parsed after the final `)` in
`stat` because the command name may contain spaces). Recheck this start identity
before signaling: a PID can be reused. A PID file alone is never authority.

- Matching root, executable, auth settings, and healthy app: reuse it, keep its
  PID and session state, and renew the preview only if necessary.
- Same app but another root, user data, or authentication policy: explain the
  difference. Ask whether to reuse it, open a different workspace within the IDE,
  start another instance on another port, or stop the old one.
- Unrelated or unknown listener: do not kill it. Offer another port or ask for
  permission after identifying it. A default port is a preference, not ownership.
- Dead recorded process: verify the port is free before restarting. Do not treat
  old metadata or an HTTP response from an unrelated app as readiness.

Run long-lived commands using the host agent's background facility. In Lathe,
`bash()` supplies log/exit sidecars when it backgrounds a command: use those
paths to inspect startup. If explicitly detaching with `nohup`, redirect stdin,
stdout, and stderr, record the new PID/start identity, and check it stayed alive.
Use bounded HTTP readiness checks, not an arbitrary sleep. Save logs outside the
served directory. On failure, inspect exit status and logs; terminate only the
newly launched, identity-verified process if cleanup is needed.

For a user-approved shutdown, recheck executable, command line, and start
identity immediately before TERM. If using a process group, create a separate
session at launch (for example `setsid`) and verify its group before signaling.
Wait for exit and port release. Do not kill all matching names, blindly signal
a saved PID, or escalate to KILL without permission. Another process may acquire
the port afterward: identify it before any restart.

## Portable operation versus Lathe preview glue

The app commands on these pages do not depend on Open WebUI. An IDE or terminal
can execute arbitrary code and read the entire sandbox; a root argument selects
a working directory, **not confinement**. Bind to loopback when the reverse proxy
can reach it. If the proxy requires a different bind address, understand direct
access routes before changing it. An app without authentication is acceptable
only behind a verified private authenticated proxy with no unauthenticated
direct path. Otherwise keep app authentication and transport security enabled.

For Lathe specifically, after local readiness:

```text
expose(target="http:8080", access="private")
```

Use the app's actual port. Shells and IDEs stay private. A public file browser or
site needs the user's explicit public/world-access request and a root containing
only intended public material. Private wrapping is not permission to ignore
other direct paths; if private registration fails, do not downgrade to public.
Check the returned URL in the user's browser, including WebSocket interaction
for IDEs/terminals. Preview registration does not start, restart, or keep an app
or sandbox awake. Recheck the app after sandbox sleep before renewing exposure.

## Offer a skill after success

Once the workflow is actually verified, **offer** to preserve it. Wait for the
user's approval and ask what they want remembered, automated, shared, or omitted.
Do not copy this handbook into a prepackaged skill or silently overwrite one.

Use `~/.local/share/lathe/skills/<name>/SKILL.md` for Lathe/Open WebUI practices
(`$XDG_DATA_HOME/lathe/skills` when configured), `~/.agents/skills/<name>/SKILL.md`
for procedures compatible agents can share, or
`<project>/.agents/skills/<name>/SKILL.md` for project knowledge. Lathe catalogs
YAML frontmatter `name:` and `description:` from existing files. Project wins
over Lathe-specific, which wins over shared on a name collision. Put detailed
instructions below the frontmatter; no credentials or expiring preview URLs.

A useful approved skill records the tested installation/version/source, exact
paths and configuration, safe startup and shutdown, readiness and process
identity checks, root and port-reuse preferences, and when to ask about upgrades.
Keep shared procedures independent of `expose()`; put Lathe-only preview steps
in a Lathe-specific skill. In a fresh chat, read the existing skill and inspect
the actual installation/listener before using its faster path. Startup should
not fetch releases or upgrade. If the skill was deleted, do not regenerate it:
after a later successful use, another offer is allowed, not automatic regrowth.
