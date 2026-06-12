from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import hook


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hook.py"
HF_TITANIC_FIXTURE = ROOT / "tests" / "fixtures" / "hf-julien-c-titanic-survival.json"


def run_hook(payload: dict[str, object], env: dict[str, str] | None = None) -> dict[str, object]:
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=ROOT,
        env=proc_env,
    )
    return json.loads(proc.stdout)


class ContextOptimizerHookTests(unittest.TestCase):
    def test_user_prompt_submit_is_invisible_by_default(self) -> None:
        output = run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(ROOT),
                "model": "gpt-5.4-mini",
                "prompt": f"inspect {HF_TITANIC_FIXTURE}",
            }
        )

        self.assertEqual(output, {})

    def test_user_prompt_submit_visible_injection_is_explicit_opt_in(self) -> None:
        output = run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(ROOT),
                "model": "gpt-5.4-mini",
                "prompt": f"inspect {HF_TITANIC_FIXTURE}",
            },
            env={"CONTEXT_OPTIMIZER_VISIBLE_PROMPT_INJECTION": "1"},
        )

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("hf-julien-c-titanic-survival.json", context)
        if "token_counter=tiktoken" in context:
            self.assertIn("Selected format: column-json", context)
            self.assertIn("Tokens: 21460 vs raw 71983 (70.2% savings)", context)
        elif "token_counter=deterministic-fallback" in context:
            self.assertRegex(context, r"Selected format: (compact-json|csv|tsv)")
            self.assertIn("Estimated tokens:", context)
        else:
            self.assertIn("Tokens:", context)

    def test_user_prompt_submit_leaves_raw_intent_unmodified(self) -> None:
        output = run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(ROOT),
                "model": "gpt-5.4-mini",
                "prompt": "show sample-data.json verbatim with line numbers",
            }
        )

        self.assertEqual(output, {})

    def test_pre_tool_use_rewrites_whole_file_context_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "sample-repetitive.json"
            data_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")
            output = run_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(tmp_path),
                    "model": "gpt-5.4-mini",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cat sample-repetitive.json"},
                }
            )
            reports = list((tmp_path / ".codex" / "context-cache" / "reports").glob("codex-pre-tool-use.*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))

        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["permissionDecision"], "allow")
        updated = hook_output["updatedInput"]["command"]
        self.assertTrue(updated.startswith("cat -- "))
        self.assertEqual(updated.count(".txt"), 1)
        self.assertNotIn("additionalContext", hook_output)
        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertEqual(report["adapter"], "codex-pre-tool-use")
        self.assertEqual(report["results"][0]["read_path"], report["results"][0]["output_path"])
        self.assertEqual(report["policy"]["min_saved_tokens"], 128)

    def test_pre_tool_use_fails_closed_when_report_verification_fails(self) -> None:
        original = hook.verify_hook_report
        hook.verify_hook_report = lambda _report_path: False
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                data_path = tmp_path / "sample-repetitive.json"
                data_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")
                stdout = StringIO()
                with redirect_stdout(stdout):
                    hook.handle_pre_tool_use(
                        {
                            "hook_event_name": "PreToolUse",
                            "cwd": str(tmp_path),
                            "model": "gpt-5.4-mini",
                            "tool_name": "Bash",
                            "tool_input": {"command": "cat sample-repetitive.json"},
                        }
                    )
        finally:
            hook.verify_hook_report = original

        self.assertEqual(json.loads(stdout.getvalue()), {})

    def test_pre_tool_use_skips_tiny_savings_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "sample-data.json"
            data_path.write_text((ROOT / "sample-data.json").read_text(), encoding="utf-8")
            output = run_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(tmp_path),
                    "model": "gpt-5.4-mini",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cat sample-data.json"},
                }
            )
            cache_dir_exists = (tmp_path / ".codex" / "context-cache").exists()

        self.assertEqual(output, {})
        self.assertFalse(cache_dir_exists)

    def test_pre_tool_use_latency_gate_skips_when_provider_is_too_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "sample-repetitive.json"
            data_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")

            output = run_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(tmp_path),
                    "model": "gpt-5.4-mini",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cat sample-repetitive.json"},
                },
                env={"CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND": "1000000000000"},
            )

            report_dir = tmp_path / ".codex" / "context-cache" / "reports"
            report_dir_exists = report_dir.exists()
            sidecars = list((tmp_path / ".codex" / "context-cache").glob("*.txt"))

        self.assertEqual(output, {})
        self.assertFalse(report_dir_exists)
        self.assertEqual(sidecars, [])

    def test_pre_tool_use_latency_gate_allows_when_savings_pay_for_local_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "sample-repetitive.json"
            data_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")

            output = run_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(tmp_path),
                    "model": "gpt-5.4-mini",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cat sample-repetitive.json"},
                },
                env={"CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND": "1"},
            )
            reports = list((tmp_path / ".codex" / "context-cache" / "reports").glob("codex-pre-tool-use.*.json"))
            report = json.loads(reports[0].read_text(encoding="utf-8"))

        self.assertIn("hookSpecificOutput", output)
        self.assertTrue(report["policy"]["latency_gate_enabled"])
        self.assertEqual(report["policy"]["provider_input_tokens_per_second"], 1.0)

    def test_pre_tool_use_does_not_discard_first_valid_rewrite_after_cold_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "sample-repetitive.json"
            data_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")
            payload = {
                "hook_event_name": "PreToolUse",
                "cwd": str(tmp_path),
                "model": "gpt-5.4-mini",
                "tool_name": "Bash",
                "tool_input": {"command": "cat sample-repetitive.json"},
            }
            stdout = StringIO()

            with (
                mock.patch.dict(os.environ, {"CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS": "1"}),
                mock.patch.object(hook, "elapsed_milliseconds", return_value=10_000.0),
                redirect_stdout(stdout),
            ):
                hook.handle_pre_tool_use(payload)

        output = json.loads(stdout.getvalue())
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertTrue(hook_output["updatedInput"]["command"].startswith("cat -- "))

    def test_pre_tool_use_leaves_semantic_operations_raw(self) -> None:
        semantic_commands = [
            "cat -n sample-data.json",
            "cat sample-data.json | head",
            "jq . sample-data.json",
        ]
        for command in semantic_commands:
            with self.subTest(command=command):
                output = run_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "cwd": str(ROOT),
                        "model": "gpt-5.4-mini",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    }
                )
                self.assertEqual(output, {})

    def test_generated_candidates_round_trip_on_real_fixtures(self) -> None:
        fixtures = [
            ROOT / "sample-data.json",
            ROOT / "sample-repetitive.json",
            HF_TITANIC_FIXTURE,
        ]
        for fixture in fixtures:
            source = hook.load_source(fixture)
            with self.subTest(fixture=fixture.name):
                candidates = hook.validated_candidates(source)
                self.assertGreaterEqual(len(candidates), 2)
                for candidate in candidates:
                    self.assertTrue(hook.candidate_matches_source(source, candidate), candidate.name)

    def test_column_json_round_trips_uniform_rows(self) -> None:
        source = hook.load_source(ROOT / "sample-data.json")
        candidates = hook.validated_candidates(source)
        candidate_names = {candidate.name for candidate in candidates}

        self.assertIn("column-json", candidate_names)
        column_json = next(candidate for candidate in candidates if candidate.name == "column-json")
        self.assertEqual(hook.decode_candidate_value(column_json.name, column_json.text, source.kind), source.value)

    def test_codebook_json_round_trips_repeated_nested_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested.json"
            path.write_text(
                json.dumps(
                    [
                        {"id": 1, "unsafe|header": "alpha", "meta": {"team": "core", "ok": True}},
                        {"id": 2, "unsafe|header": "alpha", "meta": {"team": "core", "ok": True}},
                        {"id": 3, "unsafe|header": "beta", "meta": {"team": "edge", "ok": False}},
                        {"id": 4, "unsafe|header": "beta", "meta": {"team": "edge", "ok": False}},
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            source = hook.load_source(path)
            candidates = hook.validated_candidates(source)

        codebook_json = next(candidate for candidate in candidates if candidate.name == "codebook-json")
        self.assertEqual(hook.decode_candidate_value(codebook_json.name, codebook_json.text, source.kind), source.value)

    def test_codebook_json_rejects_invalid_dictionary_column(self) -> None:
        with self.assertRaises(ValueError):
            hook.decode_candidate_value("codebook-json", '[["id"],[[1,["x"]]],[[0]]]', "json")

    def test_fallback_token_counter_uses_standard_candidates_only(self) -> None:
        source = hook.load_source(ROOT / "sample-repetitive.json")
        profile = hook.ModelProfile(
            slug="unknown",
            provider="unknown",
            tokenizer_family="fallback",
            token_counter="deterministic-fallback",
            context_window=None,
            auto_compact_token_limit=None,
            source="test",
        )

        candidate_names = {candidate.name for candidate in hook.candidates_for_profile(source, profile)}

        self.assertLessEqual(candidate_names, hook.SAFE_CANDIDATES)

    def test_choose_best_can_defer_sidecar_write_until_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_dir = tmp_path / "cache"
            source_path = tmp_path / "sample-repetitive.json"
            source_path.write_text((ROOT / "sample-repetitive.json").read_text(), encoding="utf-8")
            source = hook.load_source(source_path)
            profile = hook.resolve_model_profile("gpt-5.4-mini", {"model": "gpt-5.4-mini"}, tmp_path)

            choice = hook.choose_best(source, profile, cache_dir, persist_sidecar=False)
            self.assertIsNone(choice.output_path)
            self.assertFalse(cache_dir.exists())

            persisted = hook.choice_with_sidecar(choice, source, cache_dir, profile, hook.DEFAULT_CANDIDATE_TIER)

            self.assertIsNotNone(persisted.output_path)
            self.assertTrue(persisted.output_path.exists())

    def test_choose_best_verifies_ranked_candidate_before_selection(self) -> None:
        source = hook.SourceData(
            path=ROOT / "sample-data.json",
            kind="json",
            value={"safe": True},
            raw_text='{"safe":true}',
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
        bad = hook.Candidate("compact-json", "{}", True, "Minified JSON.")
        raw = hook.Candidate("raw", source.raw_text, True, "")

        def fake_metrics(candidate: hook.Candidate, _profile: hook.ModelProfile) -> tuple[int, int, int]:
            return (1, 1, 0) if candidate is bad else (10, 10, 0)

        with (
            mock.patch.object(hook, "generated_candidates_for_profile", return_value=[bad, raw]),
            mock.patch.object(hook, "candidate_token_metrics", side_effect=fake_metrics),
        ):
            choice = hook.choose_best(source, profile, ROOT / ".codex" / "context-cache", persist_sidecar=False)

        self.assertEqual(choice.candidate.name, "raw")
        self.assertIn("roundtrip verified", choice.candidate.notes)

    def test_tiktoken_encoding_is_cached_across_token_counts(self) -> None:
        if not hook.module_available("tiktoken"):
            self.skipTest("tiktoken is not installed")
        hook.load_tiktoken_encoding.cache_clear()
        profile = hook.ModelProfile(
            slug="gpt-5.4-mini",
            provider="openai",
            tokenizer_family="tiktoken",
            token_counter="tiktoken",
            context_window=None,
            auto_compact_token_limit=None,
            source="test",
        )

        hook.count_tokens("alpha", profile)
        before = hook.load_tiktoken_encoding.cache_info()
        hook.count_tokens("beta", profile)
        after = hook.load_tiktoken_encoding.cache_info()

        self.assertGreater(after.hits, before.hits)

    def test_non_uniform_json_does_not_emit_lossy_tabular_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "non-uniform.json"
            path.write_text(
                '[\n  {"id": 1, "name": "a", "department": null},\n  {"id": 2, "department": "sales"}\n]',
                encoding="utf-8",
            )
            source = hook.load_source(path)

            candidate_names = {candidate.name for candidate in hook.validated_candidates(source)}

        self.assertEqual(candidate_names, {"raw", "compact-json"})


if __name__ == "__main__":
    unittest.main()
