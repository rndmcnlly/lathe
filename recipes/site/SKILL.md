---
name: lathe-static-site
description: Preview a static workspace directory without installing an application.
---

# Static files

Acquire the pinned recipe snapshot using `../README.md`, then run:

```sh
python3 "$SNAPSHOT/app.py" site --root /home/daytona/workspace/my-site --port 8000
```

After READY, invoke `expose(target="http:8000", access="private")`. Only choose
public access when the user explicitly requests public/world access. Pick a
directory containing only intended public files; directory listings are enabled
where no index exists. Outside-root symlinks, including symlinked index files,
are denied. This is for trusted static content, not concurrent hostile writes.
For several sites, choose distinct ports. Root directories must already exist
inside the workspace. See the shared README for state, logs, and recovery.
