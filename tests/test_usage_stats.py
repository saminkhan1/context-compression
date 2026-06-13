from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "selector.py"
USAGE = ROOT / "scripts" / "export_usage_stats.py"


class UsageStatsExportTests(unittest.TestCase):
    def test_usage_export_aggregates_verified_reports_without_paths_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / ".codex" / "context-cache"
            report_out = out_dir / "reports" / "manual.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--report-out",
                    str(report_out),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            proc = subprocess.run(
                [sys.executable, str(USAGE), "--cwd", str(tmp_path)],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

        export = json.loads(proc.stdout)
        event = export["events"][0]

        self.assertEqual(export["schema_version"], "context-selector-usage/v1")
        self.assertEqual(export["source"]["reports_found"], 1)
        self.assertEqual(export["source"]["reports_used"], 1)
        self.assertEqual(export["summary"]["files"], 1)
        self.assertEqual(export["summary"]["selected_files"], 1)
        self.assertGreater(export["summary"]["saved_tokens"], 0)
        self.assertIn(event["selected_format"], export["by_format"])
        self.assertIn("source_sha256", event)
        self.assertNotIn("source_name", event)
        self.assertNotIn("source", event)
        self.assertNotIn("read_path", event)
        self.assertNotIn("output_path", event)

    def test_usage_export_can_include_paths_for_local_debugging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / ".codex" / "context-cache"
            report_out = out_dir / "reports" / "manual.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--report-out",
                    str(report_out),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            proc = subprocess.run(
                [sys.executable, str(USAGE), "--cwd", str(tmp_path), "--include-paths"],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

        event = json.loads(proc.stdout)["events"][0]

        self.assertIn("source", event)
        self.assertIn("source_name", event)
        self.assertIn("read_path", event)
        self.assertIn("output_path", event)
        self.assertIn("report_path", event)

    def test_usage_export_rejects_invalid_reports_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "bad.json"
            report.write_text('{"schema_version":"wrong"}', encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(USAGE), str(report)],
                text=True,
                capture_output=True,
                cwd=ROOT,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("report.schema_version must be context-selector/v1", proc.stderr)


if __name__ == "__main__":
    unittest.main()
