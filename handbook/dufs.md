# dufs: browser file transfer

Read the [handbook index](README.md) for release verification, process identity,
port conflicts, shutdown, proxy boundaries, and user-approved skill saving.
Resolve relative links against this page's URL when reading over HTTP.

Upstream: [usage](https://github.com/sigoden/dufs#readme),
[releases](https://github.com/sigoden/dufs/releases).

## Install only if needed

Inspect any existing `dufs --version`, `dufs --help`, configuration, and listener
first. Do not upgrade it merely to serve files. For blank x86_64 Linux, query
`https://api.github.com/repos/sigoden/dufs/releases/latest` with Python's
`urllib.request` or an available HTTP client. Inspect `tag_name` and `assets`;
select the actual `dufs-<tag>-x86_64-unknown-linux-musl.tar.gz` asset. The static
musl build avoids distro/glibc dependencies. If naming has changed, inspect the
current asset list rather than guessing a historical URL.

Download the selected `browser_download_url`, checking its canonical upstream
release/tag path. Verify SHA-256 against its API `digest` or the release's
published checksum asset as described in the index. If no digest is published,
explicitly discuss the provenance limitation or use an approved source. Inspect
the tar archive, extract with `filter='data'` into a new staging directory, and
install only the expected regular `dufs` binary into a user-owned versioned
location such as `~/.local/lib/dufs-TAG/dufs`. Make it executable, retain release
metadata/digest, and run `--version` and `--help`. Use a reachable approved mirror
if public metadata/assets are blocked; Lathe does not supply this executable.

## Start with explicit permissions

Select a dedicated existing absolute share directory, not the whole workspace
by default. Inspect it for secrets, symlinks, and unintended public contents.
Check port 5000 and process identity before launch, reusing only an instance
with the intended root, permissions, and auth settings. Negotiate another port
or an approved shutdown if occupied by something else.

Example behind a verified private authenticated proxy, adjusted to installed
help and the user's requested operations:

```sh
/absolute/installation/dufs /home/daytona/workspace/share \
  --bind 127.0.0.1 --port 5000 \
  --allow-upload --allow-search --allow-archive --allow-hash
```

Add `--allow-delete` only if wanted. Do not use `--allow-all` or
`--allow-symlink`: they grant more than this example. Inspect existing config
and `DUFS_*` environment variables so they cannot silently broaden permissions.
The root is not a strong isolation boundary against hostile filesystem changes.
No-auth operation needs the index's private-proxy/direct-route safeguards;
otherwise configure dufs app authentication and appropriate transport security.

Background the server with logs outside the share directory. Check the intended
instance's HTTP response with bounded retries. Current releases expose
`/__dufs__/health`; verify a small file's actual bytes as well. If uploads were
requested, test a disposable file and clean up only that file. Do not interpret
a response from an unrelated listener as successful startup.

### Lathe-only preview step

After readiness, `expose(target="http:5000", access="private")`. Verify file
download/upload in the browser. Public access requires an explicit user request
and makes the allowed operations available to others: prefer a dedicated
directory and read-only permissions where possible. Never downgrade private
exposure on failure. Follow the index's identity-checked TERM procedure for
shutdown and offer, rather than automatically save, a proven reusable skill.
