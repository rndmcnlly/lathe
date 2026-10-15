---
name: lathe-dufs
description: Start an on-demand sandbox file browser for user uploads and downloads.
---

# File browser

Read `../../README.md` and acquire the pinned sandbox support snapshot, then run:

```sh
python3 "$SNAPSHOT/scripts/apps.py" dufs --root /home/daytona/workspace --port 5000
```

Read the READY JSON, then invoke `expose(target="http:5000", access="private")`.
The browser allows upload, delete, search, ZIP download, and hashes. It does not
allow symlinks outside the selected root. Use an existing absolute workspace
directory; quote paths containing spaces. Public access requires an explicit
user request and makes this writable browser available to others. Prefer a
dedicated share directory, not the whole workspace. See the shared README for
logs, process verification, recovery, and installation receipts.
