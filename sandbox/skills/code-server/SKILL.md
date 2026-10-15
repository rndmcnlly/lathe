---
name: lathe-code-server
description: Start a private browser IDE in a sandbox workspace.
---

# Browser IDE

Read `../../README.md` and acquire the pinned sandbox support snapshot, then run:

```sh
python3 "$SNAPSHOT/scripts/apps.py" code-server --root /home/daytona/workspace --port 8080
```

After READY, invoke `expose(target="http:8080", access="private")`. Never use
public exposure: app authentication is disabled because the private preview
wrapper owns browser authentication. Generic expose has no app-specific safety
policy. The selected root is only the initial folder, not filesystem
confinement. The IDE and its terminal can access the entire sandbox, including
secrets. See the shared README for logs, verified installation, and recovery.
