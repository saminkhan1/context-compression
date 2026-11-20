from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ctab
import hook
import preview

ROOT = Path(__file__).resolve().parents[1]

_ATOMS = [1, -2.5, 0, "a", "1", "true", "", " x", "a,b", "[x", "@q", "*", "null", None, True,
          "2025-01-01", 'x"y', "Infinity", "1e5", {}, [], "a\nb", "k: v", "#x"]
_KEYS = ["a", "b", "c", ".", "d.e", "*", "", "@", "x[1]", "#n", "a{b}"]


def _value(rng: random.Random, depth: int = 0):
    t = rng.random()
    if depth > 2 or t < 0.5:
        return rng.choice(_ATOMS)
    if t < 0.75:
        return {rng.choice(_KEYS): _value(rng, depth + 1) for _ in range(rng.randint(0, 3))}
    return [_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]


class CtabTests(unittest.TestCase):
    def test_roundtrip_fuzz(self) -> None:
        rng = random.Random(7)
        for _ in range(5000):
            keys = rng.sample("pqrs", rng.randint(1, 4))
            rows = [{k: _value(rng) for k in keys if rng.random() < 0.9} for _ in range(rng.randint(2, 4))]
            doc = rng.choice([
                rows,
                {"t": rows},
                {"t": rows, "m": {rng.choice(["1", "x", "a b", "k: z", '"q', "e:", f"k{j}"]): r
                                  for j, r in enumerate(rows) if r}},
                _value(rng),
            ])
            self.assertEqual(ctab.decode(ctab.encode(doc)), doc, json.dumps(doc))

    def test_tables_group_nested_fields_and_count_lists(self) -> None:
        doc = {"orders": [
            {"id": 1, "customer": {"name": "A", "tier": "x"}, "items": [{"sku": "s"}]},
            {"id": 2, "customer": {"name": "B", "tier": "y"}, "items": []},
        ]}
        text = ctab.encode(doc)
        self.assertEqual(text.splitlines()[0], "@orders[2]")
        self.assertEqual(text.splitlines()[1], "id,customer{name,tier},#items,items")
        self.assertEqual(ctab.decode(text), doc)

    def test_length_column_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ctab.decode('@t[2]\n#xs,xs\n2,[1]\n0,[]')


class ViewHeaderTests(unittest.TestCase):
    def test_non_raw_candidates_name_the_on_disk_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "employees.json"
            path.write_text(json.dumps([{"id": i, "team": "red"} for i in range(30)], indent=2), encoding="utf-8")
            source = hook.load_source(path)
            profile = hook.resolve_model_profile("gpt-5.4-mini", {"model": "gpt-5.4-mini"}, Path(tmp))
            for candidate in hook.candidates_for_profile(source, profile, "advanced"):
                if candidate.name == "raw":
                    self.assertEqual(candidate.instructions, "")
                else:
                    self.assertIn("on disk it is a JSON array of 30 items", candidate.instructions)
                    self.assertTrue(hook.candidate_matches_source(source, candidate))


class PreviewTests(unittest.TestCase):
    def test_preview_is_small_labelled_and_question_independent(self) -> None:
        rows = [{"id": i, "level": "info", "error": {"code": i}} if i % 3 == 0 else {"id": i, "level": "warn"}
                for i in range(3000)]
        text = preview.labelled_preview({"logs": rows}, "logs.json", "a JSON object with keys logs")
        self.assertIn("NOT the full data", text)
        self.assertIn("list of 3000 objects", text)
        self.assertIn("error{code}:object (optional, in 1000/3000)", text)
        self.assertLess(len(text), 1200)

    def test_selector_emits_preview_only_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.json"
            path.write_text(json.dumps([{"id": i, "name": f"n{i}"} for i in range(500)]), encoding="utf-8")
            out = subprocess.run(
                [sys.executable, str(ROOT / "selector.py"), "--cwd", tmp, "--preview-above-tokens", "1000", "big.json"],
                capture_output=True, text=True, check=True,
            )
            result = json.loads(out.stdout)["results"][0]
            self.assertFalse(result["preview_lossless"])
            self.assertTrue(Path(result["preview_path"]).read_text().startswith("[Preview of"))
            self.assertLess(result["preview_tokens"], 300)


if __name__ == "__main__":
    unittest.main()
