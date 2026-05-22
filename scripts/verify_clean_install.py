#!/usr/bin/env python3
"""Verify the MVP from a fresh temporary checkout copy."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORE_NAMES = {
    ".git",
    ".venv",
    ".codex",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".researchclaw_cache",
    "artifacts",
    "htmlcov",
    "logs",
}
IGNORE_SUFFIXES = {".pyc", ".pyo"}
IGNORE_RELATIVE_PREFIXES = {
    Path("data") / "benchmark-corpus",
    Path("docs") / "kb",
    Path("feedback") / "local",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary checkout for debugging.",
    )
    args = parser.parse_args()

    temp_dir = Path(tempfile.mkdtemp(prefix="context-compression-clean-"))
    checkout = temp_dir / "context-compression"
    try:
        copy_checkout(ROOT, checkout)
        run("create venv", ["python3", "-m", "venv", ".venv"], checkout)
        py = checkout / ".venv" / "bin" / "python"
        run("install requirements", [str(py), "-m", "pip", "install", "-r", "requirements.txt"], checkout)
        run("chmod hook runner", ["chmod", "+x", "run-hook.sh"], checkout)
        run("unit suite", [str(py), "-m", "unittest", "discover", "-s", "tests"], checkout)
        run("four harness smokes", [str(py), "scripts/run_harness_smokes.py"], checkout)
        run("lean evidence gate", [str(py), "scripts/verify_evidence.py"], checkout)
        print(f"clean install ok: {checkout}")
        return 0
    finally:
        if args.keep_temp:
            print(f"kept temporary checkout: {checkout}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def copy_checkout(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        return {
            name
            for name in names
            if should_ignore(base / name, (base / name).relative_to(source))
        }

    shutil.copytree(source, destination, ignore=ignore)


def should_ignore(path: Path, relative: Path) -> bool:
    if any(part in IGNORE_NAMES for part in relative.parts):
        return True
    if path.suffix in IGNORE_SUFFIXES:
        return True
    return any(relative == prefix or relative.is_relative_to(prefix) for prefix in IGNORE_RELATIVE_PREFIXES)


def run(name: str, command: list[str], cwd: Path) -> None:
    print(f"[clean-install] {name}")
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"{name} failed with exit {result.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
