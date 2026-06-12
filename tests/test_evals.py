from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from evals.build_context_quality_dataset import build_cases, build_records
from evals.summarize_context_quality import build_quality_gate, markdown_summary, summarize_samples
from evals.verify_context_quality_dataset import verify_records


ROOT = Path(__file__).resolve().parents[1]


class EvalDatasetBuilderTests(unittest.TestCase):
    def test_build_cases_adds_nested_null_repeated_and_delimiter_slices(self) -> None:
        rows = [
            {
                "id": 1,
                "team": "core",
                "note": "alpha,one",
                "meta": {"region": "us", "owners": ["a", "b"]},
                "optional": None,
                "count": 2,
            },
            {
                "id": 2,
                "team": "core",
                "note": "plain",
                "meta": {"region": "eu", "owners": ["c"]},
                "optional": "set",
                "count": 3,
            },
        ]

        question_types = {case.question_type for case in build_cases(rows)}

        self.assertTrue(
            {
                "count",
                "lookup",
                "integer_sum",
                "nested_value",
                "repeated_value_count",
                "null_lookup",
                "delimiter_string",
                "first_row",
            }.issubset(question_types)
        )

    def test_build_cases_adds_missing_key_slice_for_non_uniform_rows(self) -> None:
        rows = [
            {"id": 1, "name": "alpha", "optional": None},
            {"id": 2, "name": "beta"},
        ]

        cases = {case.question_type: case for case in build_cases(rows)}

        self.assertIn("missing_key", cases)
        self.assertEqual(cases["missing_key"].target, "false")

    def test_context_quality_verifier_accepts_generated_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp)
            source = corpus / "toy.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "id": 1,
                            "team": "core",
                            "note": "alpha,one",
                            "meta": {"region": "us"},
                            "optional": None,
                            "count": 2,
                        },
                        {
                            "id": 2,
                            "team": "core",
                            "note": "plain",
                            "meta": {"region": "eu"},
                            "optional": "set",
                            "count": 3,
                        },
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            records = build_records(corpus, "gpt-5.4-mini")
            errors = verify_records(records)

        self.assertEqual(errors, [])
        self.assertTrue(all("context_format" in record for record in records))
        self.assertTrue(all("source_kind" in record for record in records))

    def test_context_quality_verifier_rejects_target_drift(self) -> None:
        records = build_records(ROOT / "tests" / "fixtures", "gpt-5.4-mini", max_files=1)
        records[0]["target"] = "__wrong__"

        errors = verify_records(records)

        self.assertTrue(any("targets differ" in error or "target mismatch" in error for error in errors))

    def test_context_quality_verifier_rejects_corrupted_optimized_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp)
            source = corpus / "toy.json"
            source.write_text(
                json.dumps(
                    [
                        {"id": 1, "team": "core", "count": 2},
                        {"id": 2, "team": "infra", "count": 3},
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            records = build_records(corpus, "gpt-5.4-mini")
            optimized = next(
                record
                for record in records
                if record["variant"] == "optimized" and record["question_type"] == "lookup"
            )
            optimized["input"] = optimized["input"].replace('"infra"', '"wrong"', 1)
            errors = verify_records(records)

        self.assertTrue(any("optimized" in error and "context target mismatch" in error for error in errors))

    def test_context_quality_summary_reports_variant_accuracy_and_regressions(self) -> None:
        samples = [
            {
                "id": "a/raw/lookup",
                "variant": "raw",
                "source_file": "a.json",
                "question_type": "lookup",
                "target": "1",
                "answer": "1",
                "correct": True,
            },
            {
                "id": "a/optimized/lookup",
                "variant": "optimized",
                "source_file": "a.json",
                "question_type": "lookup",
                "target": "1",
                "answer": "2",
                "correct": False,
            },
            {
                "id": "b/raw/count",
                "variant": "raw",
                "source_file": "b.json",
                "question_type": "count",
                "target": "2",
                "answer": "2",
                "correct": True,
            },
            {
                "id": "b/optimized/count",
                "variant": "optimized",
                "source_file": "b.json",
                "question_type": "count",
                "target": "2",
                "answer": "2",
                "correct": True,
            },
        ]

        summary = summarize_samples(samples)

        self.assertEqual(summary["by_variant"]["raw"]["accuracy"], 1.0)
        self.assertEqual(summary["by_variant"]["optimized"]["accuracy"], 0.5)
        self.assertEqual(summary["pair_parity"]["pairs"], 2)
        self.assertEqual(summary["pair_parity"]["raw_only_correct"], 1)
        self.assertEqual(summary["pair_parity"]["optimized_regressions"][0]["source_file"], "a.json")
        self.assertIn("Raw only correct", markdown_summary(summary))

        gate = build_quality_gate(
            summary,
            fail_on_optimized_regression=True,
            fail_on_missing_pairs=True,
            min_optimized_accuracy=0.99,
            min_pairs=2,
        )

        self.assertFalse(gate["passed"])
        self.assertTrue(any("optimized regressions" in failure for failure in gate["failures"]))
        self.assertTrue(any("optimized accuracy" in failure for failure in gate["failures"]))

    def test_context_quality_gate_reports_pass_for_clean_pairs(self) -> None:
        samples = [
            {
                "id": "a/raw/lookup",
                "variant": "raw",
                "source_file": "a.json",
                "question_type": "lookup",
                "target": "1",
                "answer": "1",
                "correct": True,
            },
            {
                "id": "a/optimized/lookup",
                "variant": "optimized",
                "source_file": "a.json",
                "question_type": "lookup",
                "target": "1",
                "answer": "1",
                "correct": True,
            },
        ]
        summary = summarize_samples(samples)
        summary["quality_gate"] = build_quality_gate(
            summary,
            fail_on_optimized_regression=True,
            fail_on_missing_pairs=True,
            min_optimized_accuracy=1.0,
            min_pairs=1,
        )

        self.assertTrue(summary["quality_gate"]["passed"])
        self.assertEqual(summary["quality_gate"]["failures"], [])
        self.assertIn("Status: pass", markdown_summary(summary))


if __name__ == "__main__":
    unittest.main()
