# On-demand sandbox recipes

These are ordinary sandbox programs, not Open WebUI tools. Python 3.11+ and a
blank x86_64 Linux sandbox suffice. No pip packages, curl, ss, or package manager
are needed. `site` also runs on macOS for offline tests. Runtime code remains in
`app.py`; each application's `SKILL.md` explains its use and security boundary.

## Acquire one immutable snapshot

Choose a **full 40-character commit** from a trusted Lathe release/tag. Resolve a
tag once, record its commit, and keep using that commit. No fallback to a branch,
`main`, or newer recipe code occurs. After the commit is published:

```sh
REV=FULL_40_CHARACTER_COMMIT
python3 - "$REV" <<'PY'
import re, sys, urllib.request
revision = sys.argv[1]
assert re.fullmatch('[0-9a-f]{40}', revision), 'full commit required'
url = f'https://raw.githubusercontent.com/rndmcnlly/lathe/{revision}/recipes/bootstrap.sh'
with urllib.request.urlopen(url, timeout=60) as response:
    source = response.read()
open('/tmp/lathe-recipe-bootstrap.sh', 'wb').write(source)
PY
SNAPSHOT=$(sh /tmp/lathe-recipe-bootstrap.sh "$REV")
cat "$SNAPSHOT/dufs/SKILL.md"
python3 "$SNAPSHOT/app.py" dufs --root /home/daytona/workspace --port 5000
```

Bootstrap downloads the repository's commit archive through GitHub's HTTPS API,
validates the expected layout, disallows archive links/traversal, and installs
atomically into `~/.cache/lathe/recipes/$REV/` (or
`$XDG_CACHE_HOME/lathe/recipes/$REV/`). Its only stdout is that directory.
It records a source-tree SHA-256 **beside**, not inside, the snapshot and rechecks
it before every cache reuse. It fails on changed cached bytes. Do not put runtime
state, Python bytecode, or edited files inside a snapshot.

Trust comes from choosing a reviewed commit and GitHub HTTPS, not from treating
an arbitrary recipe as safe. An independently recorded source digest can be
required with `--sha256 DIGEST`. This hashes paths and file bytes, never compressed
archive bytes; no circular self-hash or unpublished archive is needed.

## Exact unpublished trial / source manifest

Commit only the reviewed recipe sources and tests locally, then obtain `REV` via
`git rev-parse HEAD`. This does not publish anything. Upload that exact commit's
`recipes/` tree to a sandbox staging directory, for example `/tmp/recipe-source`.
Do not use a later modified working tree and label it with an earlier commit.
In the sandbox:

```sh
REV=FULL_LOCAL_40_CHARACTER_COMMIT
SOURCE=/tmp/recipe-source
DIGEST=$(sh "$SOURCE/bootstrap.sh" --hash "$SOURCE")
SNAPSHOT=$(sh "$SOURCE/bootstrap.sh" "$REV" --seed "$SOURCE" --sha256 "$DIGEST")
python3 "$SNAPSHOT/app.py" site --root /home/daytona/workspace --port 8000
```

`--hash DIRECTORY` is the reproducible source-manifest digest interface. It hashes
all regular files in lexicographic relative-path order, as UTF-8 path, NUL, and
the raw SHA-256 digest of the bytes; directory metadata and executable bits do
not matter. Symlinks fail. `--seed` follows the same layout/hash checks, lock,
atomic installation, and cache receipt as network acquisition. It never contacts
GitHub. After publication, the same commit resolves to the same source tree.

## Start, inspect, recover, expose

```sh
python3 "$SNAPSHOT/app.py" APP --root /absolute/workspace/directory --port PORT
```

`APP` is `dufs`, `ttyd`, `code-server`, or `site`. Ports must be 3000–9999.
Defaults are 5000, 7681, 8080, and 8000 respectively. Root must already exist
inside `/home/daytona/workspace`, including after symlink resolution. Paths with
spaces work. stdout is one READY JSON object with service, port, root, log, PID,
started/reused status, and `expose: "http:PORT"`. Only then call Lathe's generic
`expose(target="http:PORT", access="private")`. `app.py` does not obtain preview
URLs or know preview credentials. A public static site or file browser requires
the user's explicit request for public/world access. Never publish IDE/terminal.

State defaults to `/tmp/lathe-apps` (`--state /absolute/path` overrides it).
Executables and upstream release receipts live in `bin/APP/`; per-port
`services/PORT/` contains `log`, `process.json`, and a lock. Installation and
startup are serialized. Existing matching tracked processes are reused; dead
ones restart; another root/app on that port or an untracked listener fails.
No automatic killing of unknown processes occurs. To stop, read the PID and
verify its identity/command first, then send TERM to its process group. Retry
after stopping to recover. Readiness failure reports the log and terminates a
newly launched process. Logs append across restarts.

App installers resolve one current upstream GitHub release on first use, use
that release's asset URL and SHA-256 (ttyd uses upstream `SHA256SUMS`), and cache
the executable. dufs uses static musl, ttyd's standalone x86_64 build, and
code-server its Linux amd64 archive. `release.json` records the installed tag,
asset, and digest. Recipe-code pinning does not pin upstream app versions. Never
silently replace a cached app: choose a fresh state directory to try an update.
code-server requires the upstream Linux runtime/glibc baseline; errors are in
its log. No distro package install is attempted.

Root selection is **not sandboxing**. The terminal and IDE can access the whole
sandbox. dufs has upload/delete/search/archive/hash enabled, but not allow-all or
outside-root symlinks. Static files have no write endpoints and deny known
outside-root symlinks; they are a convenience server for trusted workspace
content, not an adversarial concurrent-filesystem security boundary.
