# Static site preview

Read the [handbook index](README.md) for process identity, occupied-port
negotiation, safe shutdown, and approved skill saving. Resolve relative links
against this page's URL when reading over HTTP.

## Inspect and serve

An existing Python 3 is enough for a small trusted preview; no package download
or Lathe-managed app is needed. Inspect `python3 -m http.server --help`. Choose
an existing absolute directory containing **only** material intended for the
audience. Check port 8000 and identify any existing listener before launching.
Reuse a healthy matching server instead of creating a duplicate. Ask about a
different root or occupied port rather than stopping somebody else's work.

```sh
python3 -m http.server 8000 --bind 127.0.0.1 \
  --directory /home/daytona/workspace/my-site
```

Run through the host agent's background facility with logs outside the served
directory. Verify readiness with bounded HTTP retries and check actual known
file bytes, not merely a successful response. Check the rendered page too.

Python's simple server has **no authentication**, enables directory listings
without an index, and **follows symlinks**, potentially serving files outside
the chosen directory. Audit the tree including symlinked directories. Do not
claim root confinement or use it for hostile concurrent filesystem activity.
For untrusted content or a less controlled environment, choose a server with
the required controls instead. Loopback works only if the intended proxy can
reach it; do not broaden binding without evaluating direct routes and auth.

## Lathe-only preview step

After readiness, `expose(target="http:8000", access="private")`. Only choose
public when the user explicitly requests public/world access, after verifying
that all reachable files and listings are intended for that audience. Failure
to create a private preview is not permission to make it public. Preview
registration does not keep the process or sandbox alive.

For shutdown, follow the index's PID/start-identity checks before TERM and
verify port release. After successful use, offer a skill recording the tested
root, command, audience, checks, and port preference, without expiring URLs.
Wait for approval; ordinary serving never automatically grows a skill.
