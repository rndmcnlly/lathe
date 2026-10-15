# ttyd: browser terminal

Read the [handbook index](README.md) for process identity, port negotiation,
source verification, shutdown, and the consent boundary for saving skills.
Resolve relative links against this page's URL when reading over HTTP.

Upstream: [usage](https://github.com/tsl0922/ttyd#readme),
[releases](https://github.com/tsl0922/ttyd/releases).

## Inspect or install

Check for an installed `ttyd`, its version/help, and any existing listener.
Reuse the installed version unless the user approves an upgrade. For a blank
x86_64 Linux sandbox, query
`https://api.github.com/repos/tsl0922/ttyd/releases/latest` with Python's
`urllib.request` or an available HTTP client. Inspect that release's assets;
the standalone x86_64 binary is normally `ttyd.x86_64`. Use its actual
`browser_download_url` and verify its canonical upstream release/tag path.

Download the release's `SHA256SUMS` when published and match the exact binary
filename (not a substring), or use the asset API's published SHA-256 digest.
Compare it with `hashlib.sha256(downloaded_bytes).hexdigest()` before making the
binary executable. If no published digest exists, discuss that limitation or
choose an approved source; a local hash alone is not verification. Install to
a new user-owned versioned directory such as `~/.local/lib/ttyd-TAG/ttyd`, keep
the release metadata/checksum receipt, and run `--version` and `--help`. Static
release binaries avoid package-manager dependencies; approved intranet packages
are also suitable. Do not assume every GitHub asset redirect is reachable.

## Start privately

A terminal permits arbitrary execution and access to sandbox secrets. A working
directory is not confinement. Keep it private, even with app authentication.
Inspect port 7681 first. Reuse only the intended executable, root, shell,
authentication policy, and process start identity. Do not restart a healthy
terminal and destroy its session just to issue a new preview URL.

After verifying the installed help, example behind a verified private
authenticated proxy with no unauthenticated direct path:

```sh
/absolute/installation/ttyd --interface 127.0.0.1 --port 7681 \
  --writable --cwd /home/daytona/workspace /bin/bash -l
```

Current ttyd defaults to read-only: `--writable` enables user input. Enable it
only for the requested interactive terminal. Do not enable URL-supplied command
arguments. If proxy-only authentication is not secure on this deployment, use
ttyd's supported app authentication and secure transport, protecting credentials
from chat/logs/process listings, or do not offer the terminal. Do not blindly
widen the bind address to fix a failed proxy connection.

Launch using the background/log procedure in the index, with bounded HTTP
readiness checks. Then verify an actual terminal WebSocket session in the
browser: HTML loading alone is insufficient. An unrelated listener on 7681
requires a new port or user-approved shutdown, not a blind kill.

### Lathe-only preview step

After readiness, `expose(target="http:7681", access="private")`. Never request
public exposure. If private registration fails, explain the blocker rather than
downgrading access. Confirm terminal input/output with the user. Follow the
index's identity-checked TERM procedure for an approved shutdown, accounting for
child shell processes. Offer to save the verified setup and port/session
preferences only after success; do not automatically create or regenerate skills.
