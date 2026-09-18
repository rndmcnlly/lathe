"""Independent public contract shared by offline and deployed-schema checks.

Do not derive expected values from Lathe: a shared implementation mistake
must not silently update its own oracle. Tuple: JSON type, required, default.
"""

EXPECTED_SCHEMA = {
    "lathe": {"manpage": ("string", False, "overview")},
    "handoff": {}, "destroy": {},
    "onboard": {"path": ("string", True, None)},
    "bash": {"command": ("string", True, None), "workdir": ("string", False, "/home/daytona/workspace"),
             "foreground_seconds": ("integer", False, -1)},
    "read": {"path": ("string", True, None), "start": ("integer", False, 1), "stop": ("integer", False, 0)},
    "write": {"path": ("string", True, None), "content": ("string", True, None)},
    "edit": {"path": ("string", True, None), "old_string": ("string", True, None),
             "new_string": ("string", True, None), "replace_all": ("boolean", False, False)},
    "glob": {"pattern": ("string", True, None), "max_lines": ("integer", False, 100)},
    "grep": {"pattern": ("string", True, None), "files": ("string", False, "**/*"),
             "max_lines": ("integer", False, 100)},
    "interpret": {"code": ("string", True, None), "timeout": ("integer", False, 120)},
    "view": {"path": ("string", True, None)},
    "delegate": {"task": ("string", True, None), "context_files": ("array", False, []),
                 "max_steps": ("integer", False, 10), "foreground_seconds": ("integer", False, -1)},
    "expose": {"target": ("string", True, None)},
}


def normalize_schema(schema):
    required = set(schema.get("required", []))
    return {name: (prop.get("type"), name in required, prop.get("default"))
            for name, prop in schema.get("properties", {}).items()}
