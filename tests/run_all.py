#!/usr/bin/env python3
"""Run every test suite and exit non-zero if any of them fails.

Suites are discovered, not listed: a hardcoded tuple meant a new
`tests/test_*.py` was never executed and CI stayed green while the thing it
covers went untested. `ORDER_FIRST` only fixes the order of the suites worth
seeing first; anything else runs after them, alphabetically.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Cheapest and most fundamental first, so a protocol break is the first thing
# on screen rather than the last.
ORDER_FIRST = (
    "test_protocol.py",
    "test_coordinator.py",
    "test_platform_logic.py",
    "test_migration.py",
)

# A hung suite must not hang CI.
SUITE_TIMEOUT = 300


def discover() -> list[str]:
    """Every test_*.py in this directory, well-known ones first."""
    found = sorted(
        os.path.basename(path) for path in glob.glob(os.path.join(HERE, "test_*.py"))
    )
    ordered = [name for name in ORDER_FIRST if name in found]
    ordered += [name for name in found if name not in ORDER_FIRST]
    return ordered


def main() -> int:
    suites = discover()
    if not suites:
        print("No test suites found - that is itself a failure.")
        return 1

    failed = []
    for suite in suites:
        print(f"\n{'=' * 70}\n== {suite}\n{'=' * 70}", flush=True)
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, suite)], timeout=SUITE_TIMEOUT
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT after {SUITE_TIMEOUT}s", flush=True)
            returncode = 1
        if returncode != 0:
            failed.append(suite)

    print(f"\n{'=' * 70}", flush=True)
    if failed:
        print("FAILED suites: " + ", ".join(failed))
        return 1
    print(f"All {len(suites)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
