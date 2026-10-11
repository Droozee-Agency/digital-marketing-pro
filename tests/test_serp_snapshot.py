"""scripts/serp-snapshot.py: a vendor-neutral SERP-feature input for rank-monitor --features.

Search Console has no per-query SERP-feature layout, so --features relied on a connected
rank-tracker MCP or a manual pass. Any SERP API or rank tracker can export that layout;
this validator reads the export and nothing else. The tests pin the format, the error
messages, and the three promises: no network, no keys, no provider names in the code.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import PLUGIN_ROOT, SCRIPTS_DIR, import_script  # noqa: E402

SCRIPT = SCRIPTS_DIR / "serp-snapshot.py"
snap = import_script("serp-snapshot.py", module_name="serp_snapshot_test")

CSV = (
    "query,gl,hl,position,ai_overview_present,ai_overview_cited,paa_present,featured_snippet_owner,source\n"
    "best trail shoes,us,en,3,true,false,yes,https://www.Rival.com/guide,export-a\n"
    "trail shoe sizing,us,en,,TRUE,true,no,shop.example.com,export-a\n"
    "waterproof trail shoes,gb,en-GB,12,false,,1,,export-a\n"
)


def run(path, *extra):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--file", str(path), *extra],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout)


class TestReadsAnyExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_csv_with_a_bom_is_normalised_and_summarised(self):
        path = self.dir / "snap.csv"
        path.write_bytes(b"\xef\xbb\xbf" + CSV.encode("utf-8"))
        code, out = run(path, "--brand-domain", "example.com")
        self.assertEqual(code, 0, out["errors"])
        first, second, third = out["rows"]
        self.assertEqual(first["position"], 3)
        self.assertEqual(first["featured_snippet_owner"], "rival.com")
        self.assertFalse(first["brand_owns_featured_snippet"])
        self.assertIsNone(second["position"], "an empty position means not ranking")
        self.assertTrue(second["ai_overview_cited"])
        self.assertTrue(second["brand_owns_featured_snippet"], "a subdomain of the brand counts")
        self.assertEqual((third["gl"], third["hl"]), ("gb", "en-GB"))
        self.assertIsNone(third["ai_overview_cited"], "an empty boolean means not reported")
        self.assertEqual(out["summary"], {"queries": 3, "ranking": 2, "ai_overview_present": 2,
                                          "ai_overview_cited": 1, "paa_present": 2,
                                          "featured_snippets": 2, "featured_snippets_owned_by_brand": 1})
        self.assertEqual(out["ignored_columns"], ["source"])

    def test_json_array(self):
        path = self.dir / "snap.json"
        path.write_text(json.dumps([{"query": "q1", "position": 1, "ai_overview_present": True,
                                     "ai_overview_cited": True, "paa_present": False}]), encoding="utf-8")
        code, out = run(path)
        self.assertEqual(code, 0, out["errors"])
        self.assertEqual(out["rows"][0]["position"], 1)
        self.assertNotIn("featured_snippets_owned_by_brand", out["summary"])

    def test_unreadable_input_exits_2(self):
        code, out = run(self.dir / "missing.csv")
        self.assertEqual(code, 2)
        self.assertIn("could not read", out["error"])
        path = self.dir / "obj.json"
        path.write_text('{"query": "q"}', encoding="utf-8")
        code, out = run(path)
        self.assertEqual(code, 2)
        self.assertIn("array", out["error"])


class TestEveryErrorNamesTheRow(unittest.TestCase):
    def errors(self, rows):
        return snap.validate(rows, "example.com")["errors"]

    def test_row_errors(self):
        cases = [
            ({"gl": "us"}, "row 1 is missing 'query'"),
            ({"query": "q", "position": "0"}, "row 1 'position' '0'"),
            ({"query": "q", "position": "2.5"}, "row 1 'position' '2.5'"),
            ({"query": "q", "paa_present": "maybe"}, "row 1 'paa_present' 'maybe' is not true/false"),
            ({"query": "q", "gl": "usa"}, "row 1 'gl' 'usa'"),
            ({"query": "q", "hl": "english"}, "row 1 'hl' 'english'"),
            ({"query": "q", "featured_snippet_owner": "not a domain"}, "is not a domain"),
            ({"query": "q", "ai_overview_present": False, "ai_overview_cited": True}, "not present"),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                errs = self.errors([row])
                self.assertTrue(any(expected in e for e in errs), errs)

    def test_duplicates_and_empty(self):
        errs = self.errors([{"query": "Trail Shoes", "gl": "us"}, {"query": "trail  shoes", "gl": "US"}])
        self.assertTrue(any("row 2 repeats row 1" in e for e in errs), errs)
        self.assertEqual(self.errors([{"query": "q", "gl": "us"}, {"query": "q", "gl": "gb"}]), [],
                         "the same query in another country is not a duplicate")
        self.assertIn("the snapshot has no rows", self.errors([]))

    def test_invalid_rows_exit_1_and_list_every_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("query,position,paa_present\n,3,yes\nq2,-1,perhaps\n", encoding="utf-8")
            code, out = run(path)
        self.assertEqual(code, 1)
        self.assertEqual(len(out["errors"]), 3, out["errors"])
        self.assertIsNone(out["summary"], "a refused snapshot gives no counts")
        self.assertIs(out["refused"], True)

    def test_a_refused_snapshot_never_counts_its_bad_row(self):
        result = snap.validate([{"query": "q", "ai_overview_present": False, "ai_overview_cited": True}])
        self.assertTrue(result["errors"])
        self.assertIsNone(result["summary"], "the impossible 'cited' row must not be counted")
        ok = snap.validate([{"query": "q", "ai_overview_present": True, "ai_overview_cited": True}])
        self.assertEqual(ok["summary"]["ai_overview_cited"], 1)
        self.assertNotIn("refused", ok)


class TestNoNetworkNoKeysNoVendor(unittest.TestCase):
    def test_no_network_or_subprocess_imports(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(imported & {"urllib", "http", "socket", "ssl", "requests", "subprocess", "ftplib"},
                         sorted(imported))

    def test_no_key_or_provider_names(self):
        text = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("api_key", text)
        self.assertNotIn("environ", text)
        for vendor in ("serpbase", "dataforseo", "semrush", "ahrefs", "serpapi", "se ranking", "moz.com"):
            self.assertNotIn(vendor, text, vendor)

    def test_the_skill_and_connectors_point_at_it(self):
        skill = (PLUGIN_ROOT / "skills" / "rank-monitor" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("serp-snapshot.py", skill)
        self.assertIn("serp-snapshot-format.md", skill)
        fmt = (PLUGIN_ROOT / "skills" / "rank-monitor" / "serp-snapshot-format.md").read_text(encoding="utf-8")
        for field in snap.FIELDS:
            self.assertIn(f"`{field}`", fmt, field)
        seo_row = next(line for line in (PLUGIN_ROOT / "CONNECTORS.md").read_text(encoding="utf-8").splitlines()
                       if re.match(r"\|\s*SEO\s*\|", line))
        self.assertIn("any SERP API (export file)", seo_row)


if __name__ == "__main__":
    unittest.main()
