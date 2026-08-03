#!/usr/bin/env python3
"""Run every test suite and exit non-zero if any of them fails."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ("test_protocol.py", "test_coordinator.py", "test_platform_logic.py")


def main() -> int:
    failed = []
    for suite in SUITES:
        print(f"\n{'=' * 70}\n== {suite}\n{'=' * 70}")
        result = subprocess.run([sys.executable, os.path.join(HERE, suite)])
        if result.returncode != 0:
            failed.append(suite)

    print(f"\n{'=' * 70}")
    if failed:
        print("FAILED suites: " + ", ".join(failed))
        return 1
    print(f"All {len(SUITES)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
