#!/usr/bin/env python3
"""
Dispatcher for lathe test suites.

Backwards-compatible entry point that delegates to the three focused
test files:

    test_unit.py         — pure Python, no network
    test_integration.py  — live Daytona sandbox API
    test_deployment.py   — live OWUI instance via Socket.IO

Usage:
    uv run --script test_harness.py              # unit + integration
    uv run --script test_harness.py unit         # unit tests only
    uv run --script test_harness.py integration  # integration tests only
    uv run --script test_harness.py deployment   # deployment tests only
    uv run --script test_harness.py all          # everything
"""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = {
    "unit": os.path.join(HERE, "test_unit.py"),
    "integration": os.path.join(HERE, "test_integration.py"),
    "deployment": os.path.join(HERE, "test_deployment.py"),
}


def run_suite(name: str, extra_args: list[str] = []) -> int:
    path = SUITES[name]
    cmd = ["uv", "run", "--script", path] + extra_args
    print(f"\n{'─'*60}")
    print(f"Running: {' '.join(cmd)}")
    print(f"{'─'*60}", flush=True)
    return subprocess.call(cmd)


def main():
    args = sys.argv[1:]

    if "--list" in args:
        print("Available suites:")
        for name in SUITES:
            print(f"  {name}")
        print("\nPass suite names, or 'all' for everything.")
        print("Default (no args): unit + integration")
        return

    if not args:
        # Backwards compat: no args = unit + integration (like before)
        suites = ["unit", "integration"]
    elif "all" in args:
        suites = list(SUITES.keys())
    else:
        suites = []
        extra = []
        for arg in args:
            if arg in SUITES:
                suites.append(arg)
            else:
                extra.append(arg)
        if not suites:
            # Bare test names → assume integration (backwards compat)
            suites = ["integration"]

    failed = []
    for name in suites:
        rc = run_suite(name)
        if rc != 0:
            failed.append(name)

    if failed:
        print(f"\nFAILED suites: {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"\nAll {len(suites)} suite(s) passed.")


if __name__ == "__main__":
    main()
