# code-server: browser IDE

Read the [handbook index](README.md) for source verification, process identity,
port negotiation, safe shutdown, preview boundaries, and consent to save skills.
Resolve this relative link against this page's URL when reading over HTTP.

Upstream references:
[installation](https://github.com/coder/code-server/blob/main/docs/install.md),
[FAQ](https://github.com/coder/code-server/blob/main/docs/FAQ.md),
[releases](https://github.com/coder/code-server/releases).

## Inspect or install once

First look for `command -v code-server`, existing standalone installations,
configuration, and a running instance. Ask before upgrading or replacing them.
The Linux amd64 standalone archive bundles Node and needs a compatible glibc
runtime. Upstream currently documents glibc >= 2.28 and GLIBCXX >= 3.4.21;
check current upstream requirements and actual runtime errors, not just `uname`.
An intranet-provided binary or package is equally valid if approved.

For a blank x86_64 Linux sandbox, this Python-only example discovers a stable
release and downloads its exact standalone asset, verifying the API's published
digest. It does not install system packages or use a privileged install script:

```sh
python3 - <<'PY'
import hashlib, json, pathlib, platform, re, urllib.request
assert platform.system() == 'Linux' and platform.machine() in ('x86_64', 'AMD64')
def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sandbox-app-setup'})
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()
release = json.loads(fetch('https://api.github.com/repos/coder/code-server/releases/latest'))
tag = release['tag_name']
assert re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag), 'inspect unexpected release tag'
name = f'code-server-{tag[1:]}-linux-amd64.tar.gz'
assets = [a for a in release['assets'] if a['name'] == name]
assert len(assets) == 1, 'inspect current asset list and upstream requirements'
asset = assets[0]
url = asset['browser_download_url']
assert url == f'https://github.com/coder/code-server/releases/download/{tag}/{name}'
digest = asset.get('digest', '').removeprefix('sha256:')
assert re.fullmatch('[0-9a-f]{64}', digest), 'consult published checksums or an approved mirror; do not silently skip verification'
data = fetch(url)
assert hashlib.sha256(data).hexdigest() == digest, 'download digest mismatch'
staging = pathlib.Path.home() / '.local/share/code-server-downloads' / tag
staging.mkdir(parents=True, exist_ok=False)
(staging / name).write_bytes(data)
(staging / 'release.json').write_text(json.dumps(release, indent=2))
print(staging / name)
PY
```

If the API omits `digest`, inspect that release's checksum assets and verify the
exact filename against its published SHA-256, or use an approved mirror. Do not
rerun this first-install snippet for a routine startup. Keep the printed path;
tool calls may not share shell variables. On interrupted setup, inspect existing
staging rather than blindly replacing it.

Inspect the archive member names and links, then extract into a new user-owned
directory such as `~/.local/lib/code-server-VERSION/`. With Python 3.11+ that
supports extraction filters, `tarfile.open(archive).extractall(new_directory,
filter='data')` rejects traversal and escaping links while allowing the package's
internal links. Do not use an unfiltered extraction fallback. The archive
normally contains a `code-server-VERSION-linux-amd64/` top-level directory;
locate its actual `bin/code-server` and record that absolute path. Run it with
`--version` and `--help`. A missing glibc/libstdc++ dependency calls for an
approved compatible runtime/source, not a silent version downgrade.

## Start or reuse, without disturbing work

Default port 8080 is only a suggestion. Follow the index's listener checks
first. Inspect code-server's process tree, command line, start identity,
workspace, config path, and user-data directory. Reuse a compatible healthy
instance, preserving the PID and open IDE state. A new requested folder does
not authorize killing an existing IDE: offer opening that folder in the current
IDE, a second instance on another port with separate data/config where needed,
or a user-approved shutdown. An unrelated listener requires negotiation, not
`pkill`, `fuser -k`, or blind PID killing.

After checking the installed help, a fresh instance behind a verified private
authenticated proxy can use this command shape. Replace the executable and
workspace paths with the inspected absolute paths:

```sh
/absolute/installation/bin/code-server \
  --bind-addr 127.0.0.1:8080 --auth none \
  --disable-telemetry /home/daytona/workspace
```

`--auth none` is **only** appropriate if the proxy can reach this listener and
no unauthenticated direct route exists. Otherwise retain code-server's password
authentication (inspect `~/.config/code-server/config.yaml`, or use a dedicated
`--config` file). Do not print its password into chat or a skill. Verify the
effective bind/auth settings in logs: environment variables and existing config
can affect behavior. If loopback cannot be proxied, do not blindly widen binding
with authentication disabled. Keep the IDE private even with app authentication.

Use background execution with logs and recorded process identity as described
in the index. Wait with a bounded retry loop for an HTTP response from the
intended instance at `http://127.0.0.1:8080/`; inspect startup logs and the
process if it fails. For password mode a login redirect/page may be healthy,
not evidence that authentication should be removed. Verify the IDE in the
browser, including a working terminal or another WebSocket-backed interaction.

### Lathe-only preview step

After local readiness, request
`expose(target="http:8080", access="private")`. A failed private registration
must not lead to public exposure. Verify the final URL loads the expected
workspace. Keep preview URLs out of saved skills because they expire.

For shutdown, identify the actual code-server process or deliberately isolated
process group, recheck its start identity, send TERM only after approval, then
wait for exit and port release. Do not kill all Node processes or assume a
launcher PID and its children are interchangeable. Restart after sandbox sleep
only when no matching instance remains.

## After verified success

Offer to save the tested procedure at the scope the user chooses. Record exact
binary/version, source receipt path, config/data paths, auth policy without
secrets, root and port preferences, process checks, startup/readiness/stop
commands, and upgrade preferences. In a fresh chat, read that skill and reuse
the installed version: no release lookup, reinstall, or upgrade is needed for
normal startup. A running instance should take the reuse/negotiation path above.
