from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hook
import selector
import verify_selector_report


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "selector.py"
VERIFY = ROOT / "verify_selector_report.py"


class SelectorCliTests(unittest.TestCase):
    def test_selector_cli_emits_stable_decision_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            report_out = Path(tmp) / "reports" / "selector-report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--report-out",
                    str(report_out),
                    "--adapter",
                    "test-adapter",
                    "--model",
                    "gpt-5.4-mini",
                    "--include-candidates",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            persisted = json.loads(report_out.read_text())
            result = report["results"][0]
            self.assertTrue(Path(result["output_path"]).exists())
            self.assertEqual(result["output_sha256"], hook.sha256_file(Path(result["output_path"])))
            self.assertEqual(verify_selector_report.validate_report(report, check_files=True), [])

        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertEqual(report["adapter"], "test-adapter")
        self.assertEqual(persisted, report)
        self.assert_selector_contract(report)
        self.assertEqual(report["summary"]["files"], 1)
        self.assertEqual(report["summary"]["selected_files"], 1)
        self.assertTrue(result["selected"])
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["read_path"], result["output_path"])
        self.assertIn("sha256", result)
        self.assertIn("output_sha256", result)
        self.assertIn("candidates", result)

    def test_selector_cli_reports_unsupported_files_without_failure(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SELECTOR),
                "--cwd",
                str(ROOT),
                "README.md",
            ],
            text=True,
            capture_output=True,
            check=True,
            cwd=ROOT,
        )

        report = json.loads(proc.stdout)

        self.assertEqual(report["summary"]["files"], 1)
        self.assert_selector_contract(report)
        self.assertEqual(report["summary"]["selected_files"], 0)
        self.assertEqual(report["results"][0]["decision"], "unsupported_format")
        self.assertEqual(report["results"][0]["read_path"], str((ROOT / "README.md").resolve()))
        self.assertEqual(verify_selector_report.validate_report(report), [])

    def test_selector_cli_keeps_raw_read_path_when_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "--min-savings-ratio",
                    "0.99",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            result = report["results"][0]
            self.assertFalse(out_dir.exists() and any(out_dir.iterdir()))

        self.assertFalse(result["selected"])
        self.assert_selector_contract(report)
        self.assertEqual(result["decision"], "below_threshold")
        self.assertIsNone(result["output_path"])
        self.assertIsNone(result["output_sha256"])
        self.assertEqual(result["read_path"], str((ROOT / "sample-repetitive.json").resolve()))
        self.assertEqual(report["summary"]["selected_files"], 0)
        self.assertEqual(report["summary"]["selected_tokens"], report["summary"]["raw_tokens"])
        self.assertEqual(report["summary"]["saved_tokens"], 0)
        self.assertEqual(verify_selector_report.validate_report(report, check_files=True), [])

    def test_selector_cli_keeps_raw_read_path_when_below_min_saved_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "--min-saved-tokens",
                    "999999",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            result = report["results"][0]
            self.assertFalse(out_dir.exists() and any(out_dir.iterdir()))

        self.assertFalse(result["selected"])
        self.assertEqual(result["decision"], "below_min_saved_tokens")
        self.assertIsNone(result["output_path"])
        self.assertIsNone(result["output_sha256"])
        self.assertEqual(result["read_path"], str((ROOT / "sample-repetitive.json").resolve()))
        self.assertEqual(report["policy"]["min_saved_tokens"], 999999)
        self.assertEqual(verify_selector_report.validate_report(report, check_files=True), [])

    def test_report_verifier_rejects_unsafe_selected_read_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            report["results"][0]["read_path"] = str(ROOT / "sample-repetitive.json")

        errors = verify_selector_report.validate_report(report)

        self.assertTrue(any("read_path must equal output_path" in error for error in errors))

    def test_report_verifier_rejects_sidecar_that_does_not_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            Path(report["results"][0]["output_path"]).write_text("Minified JSON.\n[]", encoding="utf-8")
            errors = verify_selector_report.validate_report(report, check_files=True)

        self.assertTrue(any("round-trip" in error for error in errors))

    def test_report_verifier_rejects_sidecar_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            report["results"][0]["output_sha256"] = "0" * 64
            errors = verify_selector_report.validate_report(report, check_files=True)

        self.assertTrue(any("output_sha256 does not match" in error for error in errors))

    def test_report_verifier_rejects_missing_policy_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR),
                    "--cwd",
                    str(ROOT),
                    "--out-dir",
                    str(out_dir),
                    "--model",
                    "gpt-5.4-mini",
                    "sample-repetitive.json",
                ],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

            report = json.loads(proc.stdout)
            del report["policy"]["min_saved_tokens"]
            errors = verify_selector_report.validate_report(report)

        self.assertTrue(any("policy.min_saved_tokens is required" in error for error in errors))

    def test_report_verifier_cli_accepts_selector_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "cache"
            report_out = Path(tmp) / "selector-report.json"
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
                [sys.executable, str(VERIFY), "--check-files", str(report_out)],
                text=True,
                capture_output=True,
                check=True,
                cwd=ROOT,
            )

        self.assertIn("context-selector/v1 report ok", proc.stdout)

    def test_context_selector_schema_is_parseable_and_mentions_read_path(self) -> None:
        schema = json.loads((ROOT / "docs" / "schemas" / "context-selector-v1.schema.json").read_text())

        self.assertEqual(schema["properties"]["schema_version"]["const"], "context-selector/v1")
        result_properties = schema["properties"]["results"]["items"]["properties"]
        result_required = schema["properties"]["results"]["items"]["required"]
        policy_required = schema["properties"]["policy"]["required"]
        self.assertIn("read_path", result_required)
        self.assertIn("read_path", result_properties)
        self.assertIn("output_path", result_properties)
        self.assertIn("output_sha256", result_properties)
        self.assertIn("candidate_tier", policy_required)
        self.assertIn("min_saved_tokens", policy_required)

    def test_candidate_metrics_reuses_supplied_raw_token_count(self) -> None:
        source = hook.SourceData(
            path=ROOT / "sample-data.json",
            kind="json",
            value=[],
            raw_text="[]",
        )
        profile = hook.ModelProfile(
            slug="test",
            provider="test",
            tokenizer_family="fallback",
            token_counter="deterministic-fallback",
            context_window=None,
            auto_compact_token_limit=None,
            source="test",
        )
        candidate = hook.Candidate("compact-json", "[]", True, "Minified JSON.")

        with (
            mock.patch.object(selector.hook, "candidates_for_profile", return_value=[candidate]),
            mock.patch.object(selector.hook, "candidate_token_metrics", return_value=(5, 2, 3)),
            mock.patch.object(selector.hook, "count_tokens", side_effect=AssertionError("raw tokens were recounted")),
        ):
            rows = selector.candidate_metrics(source, profile, "safe", raw_tokens=42)

        self.assertEqual(rows[0]["saved_tokens"], 37)
        self.assertEqual(rows[0]["savings_ratio"], 1.0 - (5 / 42))

    def assert_selector_contract(self, report: dict[str, object]) -> None:
        self.assertEqual(report["schema_version"], "context-selector/v1")
        for result in report["results"]:
            self.assertIn("source", result)
            self.assertIn("source_name", result)
            self.assertIn("selected", result)
            self.assertIn("decision", result)
            self.assertIn("read_path", result)
            self.assertIsInstance(result["read_path"], str)
            if result["selected"]:
                self.assertEqual(result["read_path"], result["output_path"])
                self.assertIn("output_sha256", result)
            elif "output_path" in result:
                self.assertIsNone(result["output_path"])
                self.assertIsNone(result["output_sha256"])


if __name__ == "__main__":
    unittest.main()
