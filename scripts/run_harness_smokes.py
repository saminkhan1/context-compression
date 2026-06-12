#!/usr/bin/env python3
"""Run the harness smoke tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests.test_harness_smokes.HarnessSmokeTests.test_codex_pretooluse_smoke_rewrites_to_verified_sidecar",
    "tests.test_harness_smokes.HarnessSmokeTests.test_claude_code_read_smoke_rewrites_to_verified_sidecar",
    "tests.test_harness_smokes.HarnessSmokeTests.test_claude_code_bash_smoke_rewrites_cat_to_verified_sidecar",
    "tests.test_harness_smokes.HarnessSmokeTests.test_pi_smoke_returns_verified_report_with_selected_read_path",
    "tests.test_harness_smokes.HarnessSmokeTests.test_openclaw_smoke_returns_verified_report_with_selected_read_path",
    "tests.test_harness_smokes.HarnessSmokeTests.test_hermes_agent_plugin_smoke_overrides_read_file_to_verified_sidecar",
]


def main() -> int:
    for test_name in TESTS:
        print(f"[smoke] {test_name}")
        subprocess.run(
            [sys.executable, "-m", "unittest", test_name],
            cwd=ROOT,
            check=True,
        )
    print("[smoke] all harness smokes passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
