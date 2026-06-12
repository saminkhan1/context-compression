from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import benchmark
import hook
from scripts.verify_evidence import validate_benchmark_smoke, validate_hook_rewrite


class BenchmarkTests(unittest.TestCase):
    def test_existing_source_summary_reuses_complete_local_files(self) -> None:
        rows = [{"id": 1, "label": "alpha"}, {"id": 2, "label": "beta"}]
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            benchmark.write_source_files(out_dir, "toy", rows, ("json", "jsonl", "csv", "tsv"))

            summary = benchmark.existing_source_summary(
                out_dir,
                "toy",
                ("json", "jsonl", "csv", "tsv"),
                min_rows=2,
                force_download=False,
            )

            self.assertEqual(summary, {"rows_written": 2, "reused_existing": True})
            self.assertIsNone(
                benchmark.existing_source_summary(
                    out_dir,
                    "toy",
                    ("json", "jsonl", "csv", "tsv"),
                    min_rows=2,
                    force_download=True,
                )
            )

    def test_run_benchmark_writes_json_and_markdown_reports(self) -> None:
        rows = [{"id": 1, "team": "red"}, {"id": 2, "team": "blue"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            benchmark.write_source_files(corpus, "toy", rows, ("json", "jsonl"))
            baseline_dir = root / "toon"
            baseline_dir.mkdir()
            (baseline_dir / "toy.json.txt").write_text("baseline-json", encoding="utf-8")

            json_out = root / "report.json"
            markdown_out = root / "report.md"
            with redirect_stdout(StringIO()):
                benchmark.run_benchmark(
                    SimpleNamespace(
                        corpus=corpus,
                        model="gpt-5.4-mini",
                        input_price_per_1m=5.0,
                        monthly_calls=100,
                        provider_input_tokens_per_second=500.0,
                        baseline_dir=[baseline_dir],
                        baseline_command=[],
                        baseline_out_dir=None,
                        require_publication_corpus=False,
                        json_out=json_out,
                        markdown_out=markdown_out,
                    )
                )

            report = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["files"], 2)
            self.assertIn("by_extension", report["totals"])
            self.assertIn("by_source", report["totals"])
            self.assertEqual(report["totals"]["by_source"]["toy"]["files"], 2)
            self.assertIn("best_formats", report["totals"]["by_source"]["toy"])
            self.assertIn("latency", report["totals"])
            self.assertIn("break_even", report["totals"]["latency"])
            self.assertIn("corpus_fingerprint", report)
            self.assertEqual(report["baseline_provenance"]["toon"]["source"], "directory")
            self.assertIn("external_baselines", report["totals"])
            self.assertEqual(report["totals"]["external_baselines"]["toon"]["files_available"], 1)
            self.assertIn("candidate_ablation", report["totals"])
            self.assertGreaterEqual(report["totals"]["candidate_ablation"]["raw"]["files_available"], 2)
            self.assertIn("average_rank", report["totals"]["candidate_ablation"]["raw"])
            self.assertIn("total_milliseconds", report["results"][0]["latency"])
            self.assertIn("break_even", report["results"][0]["latency"])
            self.assertIn("external_baselines", report["results"][0])
            markdown = markdown_out.read_text(encoding="utf-8")
            self.assertIn("# Context Compression Benchmark Report", markdown)
            self.assertIn("## External Baselines", markdown)
            self.assertIn("## Candidate Ablation", markdown)
            self.assertIn("## By Source Dataset", markdown)
            self.assertIn("## Projected Latency At Configured Throughput", markdown)
            self.assertLess(markdown.index("| `json` |"), markdown.index("## Projected Latency"))

    def test_run_benchmark_can_generate_command_baselines(self) -> None:
        rows = [{"id": 1, "team": "red"}, {"id": 2, "team": "blue"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            benchmark.write_source_files(corpus, "toy", rows, ("json",))

            json_out = root / "report.json"
            markdown_out = root / "report.md"
            generated_dir = root / "generated"
            with redirect_stdout(StringIO()):
                benchmark.run_benchmark(
                    SimpleNamespace(
                        corpus=corpus,
                        model="gpt-5.4-mini",
                        input_price_per_1m=5.0,
                        monthly_calls=100,
                        provider_input_tokens_per_second=0.0,
                        baseline_dir=[],
                        baseline_command=["rawcopy=cp {input} {output}"],
                        baseline_out_dir=generated_dir,
                        require_publication_corpus=False,
                        json_out=json_out,
                        markdown_out=markdown_out,
                    )
                )

            report = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(report["baseline_dirs"]["rawcopy"], str((generated_dir / "rawcopy").resolve()))
            provenance = report["baseline_provenance"]["rawcopy"]
            self.assertEqual(provenance["source"], "command")
            self.assertEqual(provenance["command_template"], "cp {input} {output}")
            manifest_path = Path(provenance["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "baseline-provenance/v1")
            self.assertEqual(manifest["name"], "rawcopy")
            self.assertEqual(manifest["command_template"], "cp {input} {output}")
            self.assertEqual(len(manifest["files"]), 1)
            self.assertEqual(manifest["files"][0]["source_sha256"], hook.sha256_file(corpus / "toy.json"))
            self.assertEqual(
                manifest["files"][0]["output_sha256"],
                hook.sha256_file(generated_dir / "rawcopy" / "toy.json.txt"),
            )
            self.assertEqual(report["totals"]["external_baselines"]["rawcopy"]["files_available"], 1)
            self.assertTrue((generated_dir / "rawcopy" / "toy.json.txt").exists())

    def test_publication_corpus_validator_accepts_current_hf_corpus(self) -> None:
        corpus = Path("data/benchmark-corpus")
        if corpus.exists():
            self.assertEqual(benchmark.validate_publication_corpus(corpus), [])

    def test_publication_corpus_includes_paper_aligned_loghub(self) -> None:
        spec = next(spec for spec in benchmark.HF_DATASETS if spec.slug == "hf-loghub-2")

        self.assertEqual(spec.dataset, "bolu61/loghub_2")
        self.assertIn("logs", spec.shape_tags)
        self.assertIn("repetitive", spec.shape_tags)
        self.assertIn("paper-aligned", spec.benchmark_role)

    def test_publication_corpus_validator_rejects_toy_corpus(self) -> None:
        rows = [{"id": 1, "team": "red"}]
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp)
            benchmark.write_source_files(corpus, "toy", rows, ("json",))
            (corpus / "manifest.json").write_text(
                json.dumps(
                    {
                        "rows_per_source_requested": 1,
                        "formats": ["json"],
                        "sources": [{"slug": "toy", "rows_written": 1}],
                    }
                ),
                encoding="utf-8",
            )

            errors = benchmark.validate_publication_corpus(corpus)

        self.assertTrue(any("Hugging Face sources" in error for error in errors))

    def test_publication_corpus_validator_rejects_missing_shape_coverage(self) -> None:
        rows = [{"id": 1, "team": "red"}]
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp)
            sources = []
            for spec in benchmark.HF_DATASETS:
                benchmark.write_source_files(corpus, spec.slug, rows * (spec.minimum_rows or 1000), ("json", "jsonl", "csv", "tsv"))
                spec_record = benchmark.dataset_spec_dict(spec)
                spec_record["rows_written"] = spec.minimum_rows or 1000
                spec_record["shape_tags"] = ["flat"]
                sources.append(spec_record)
            (corpus / "manifest.json").write_text(
                json.dumps(
                    {
                        "rows_per_source_requested": 1000,
                        "formats": ["json", "jsonl", "csv", "tsv"],
                        "sources": sources,
                    }
                ),
                encoding="utf-8",
            )

            errors = benchmark.validate_publication_corpus(corpus)

        self.assertTrue(any("shape coverage" in error for error in errors))

    def test_resolve_baseline_command_specs_validates_name_and_template(self) -> None:
        specs = benchmark.resolve_baseline_command_specs(["toon=encode {input} {output}"])
        self.assertEqual(specs[0].name, "toon")
        self.assertEqual(specs[0].command_template, "encode {input} {output}")

        with self.assertRaises(SystemExit):
            benchmark.resolve_baseline_command_specs(["bad name=encode {input} {output}"])
        with self.assertRaises(SystemExit):
            benchmark.resolve_baseline_command_specs(["missing-separator"])

    def test_resolve_baseline_file_accepts_plain_or_txt_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baseline_dir = Path(tmp)
            direct = baseline_dir / "toy.json"
            direct.write_text("a", encoding="utf-8")
            self.assertEqual(benchmark.resolve_baseline_file(baseline_dir, "toy.json"), direct)
            direct.unlink()
            txt = baseline_dir / "toy.json.txt"
            txt.write_text("b", encoding="utf-8")
            self.assertEqual(benchmark.resolve_baseline_file(baseline_dir, "toy.json"), txt)

    def test_parse_formats_rejects_unsupported_formats(self) -> None:
        with self.assertRaises(SystemExit):
            benchmark.parse_formats("json,xml")

    def test_evidence_gate_validates_benchmark_smoke_fields(self) -> None:
        report = {
            "totals": {
                "latency": {},
                "external_baselines": {
                    "rawcopy": {
                        "files_available": 1,
                        "files_missing": 0,
                    }
                },
                "candidate_ablation": {"raw": {"files_available": 1}},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            validate_benchmark_smoke(path)

            report["totals"]["external_baselines"]["rawcopy"]["files_missing"] = 1
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(SystemExit):
                validate_benchmark_smoke(path)

            report["totals"]["external_baselines"]["rawcopy"]["files_missing"] = 0
            report["totals"]["external_baselines"]["toon"] = {
                "files_available": 1,
                "files_missing": 0,
            }
            path.write_text(json.dumps(report), encoding="utf-8")
            validate_benchmark_smoke(path, baseline="toon")

    def test_toon_baseline_helper_documents_round_trip_and_optional_dependency(self) -> None:
        script = Path("scripts/toon_baseline.mjs").read_text(encoding="utf-8")

        self.assertIn("@toon-format/toon", script)
        self.assertIn("toon.encode", script)
        self.assertIn("toon.decode", script)
        self.assertIn("TOON round-trip changed the parsed source value", script)
        self.assertIn("--fallback-raw-on-fail", script)
        self.assertIn("Benchmark-only TOON encoder", script)

    def test_evidence_gate_requires_actual_hook_rewrite(self) -> None:
        validate_hook_rewrite(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": {
                            "command": "cat -- /repo/.codex/context-cache/sample-repetitive.abc.column-json.txt"
                        },
                    }
                }
            )
        )

        with self.assertRaises(SystemExit):
            validate_hook_rewrite("{}")
        with self.assertRaises(SystemExit):
            validate_hook_rewrite(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "updatedInput": {"command": "cat sample-repetitive.json"},
                        }
                    }
                )
            )
        with self.assertRaises(SystemExit):
            validate_hook_rewrite(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "updatedInput": {
                                "command": "cat -- /repo/.codex/context-cache/sample-repetitive.abc.column-json.txt"
                            },
                            "additionalContext": "not invisible",
                        }
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
