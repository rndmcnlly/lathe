---
name: lathe-ttyd
description: Start a private interactive browser terminal in a sandbox.
---

# Browser terminal

Acquire the pinned recipe snapshot using `../README.md`, then run:

```sh
python3 "$SNAPSHOT/app.py" ttyd --root /home/daytona/workspace --port 7681
```

After READY, invoke `expose(target="http:7681", access="private")`. Never use
public exposure: the service has no app authentication and permits full shell
access. The root only selects the shell's working directory, not confinement.
The user can edit files, run programs, and read sandbox secrets. Private wrapper
authentication is essential; generic expose does not enforce app-specific
restrictions. See the shared README for logs, process verification, and recovery.
