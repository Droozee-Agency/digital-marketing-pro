"""Scripts read files with or without a UTF-8 BOM, never write one, and print runnable hints.

A fresh-install smoke test (2026-10-12, final pass) fed every agent- or user-written input
twice, plain and BOM-prefixed (Excel's "CSV UTF-8" export and PowerShell 5.1's Out-File /
Set-Content -Encoding utf8 both write a BOM):
  * utm-generator.py --csv read the BOM into the first header ("\\ufeffbase_url"), so every
    row reported "Invalid URL: no domain found" with base_url null, and exited 0;
  * connector_executor.py --prepare-batch refused the plan ("Unexpected UTF-8 BOM").
Every text read in scripts/ now uses utf-8-sig (it reads plain UTF-8 unchanged), every text
write names utf-8 (no BOM; no platform-default cp1252 on Windows), and printed hints run a
script by the interpreter and its absolute path rather than `python scripts/x.py`.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SCRIPTS_DIR  # noqa: E402

sys.path.insert(0, str(SCRIPTS_DIR))
import _common  # noqa: E402

BOM = b"\xef\xbb\xbf"


def call_kind(node):
    """'read' / 'write' for a text-mode file call, else None (binary, network, other)."""
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
    if name == "read_text":
        return "read"
    if name == "write_text":
        return "write"
    if name != "open" or any(kw.arg == "timeout" for kw in node.keywords):
        return None
    if isinstance(f, ast.Attribute):
        if "opener" in ast.dump(f.value).lower():
            return None
        mode_arg = (node.args[1] if len(node.args) > 1 else None) if (
            isinstance(f.value, ast.Name) and f.value.id in ("io", "codecs")) else (node.args[0] if node.args else None)
    else:
        mode_arg = node.args[1] if len(node.args) > 1 else None
    mode = mode_arg.value if isinstance(mode_arg, ast.Constant) else None
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    mode = mode or "r"
    if not isinstance(mode, str) or "b" in mode:
        return None
    return "write" if any(c in mode for c in "wax+") else "read"


def encodings():
    for p in sorted(SCRIPTS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Call) and (kind := call_kind(node)):
                enc = next((kw.value.value for kw in node.keywords
                            if kw.arg == "encoding" and isinstance(kw.value, ast.Constant)), None)
                yield p.name, node.lineno, kind, enc


class TestEveryReadAcceptsABomAndNoWriteMakesOne(unittest.TestCase):
    def test_reads_use_utf8_sig_and_writes_use_utf8(self):
        seen, bad = {"read": 0, "write": 0}, []
        for name, line, kind, enc in encodings():
            seen[kind] += 1
            want = "utf-8-sig" if kind == "read" else "utf-8"
            if enc != want:
                bad.append(f"{name}:{line} {kind} encoding={enc!r} (want {want!r})")
        self.assertGreater(seen["read"], 100, "the scan found almost no reads; it is broken")
        self.assertGreater(seen["write"], 30)
        self.assertEqual(bad, [], "\n  " + "\n  ".join(bad))

    def test_plant_the_scanner_sees_a_plain_utf8_read_and_a_bom_write(self):
        tree = ast.parse('open(p, encoding="utf-8").read()\nPath(p).write_text(s, encoding="utf-8-sig")\n'
                         'urllib.request.build_opener(x).open(req, timeout=5)\n')
        kinds = [(call_kind(n), n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Call) and call_kind(n)]
        self.assertEqual(sorted(kinds), [("read", 1), ("write", 2)], "a network open is not a file read")


def run(args, env=None, cwd=None):
    proc = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=cwd, timeout=120)
    return proc.returncode, proc.stdout


class TestBomInputs(unittest.TestCase):
    CSV = ("base_url,source,medium,campaign\n"
           "https://example.com/spring,newsletter,email,spring-sale\n"
           "https://example.com/pricing,linkedin,social,q2-launch\n")

    def test_utm_csv_gives_the_same_result_with_and_without_a_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, bom = Path(tmp) / "plain.csv", Path(tmp) / "bom.csv"
            plain.write_bytes(self.CSV.encode("utf-8"))
            bom.write_bytes(BOM + self.CSV.encode("utf-8"))
            outs = []
            for path in (plain, bom):
                code, out = run([str(SCRIPTS_DIR / "utm-generator.py"), "--csv", str(path)])
                self.assertEqual(code, 0, out)
                outs.append(json.loads(out))
        self.assertEqual(outs[0], outs[1], "a BOM must not change the result")
        urls = [r.get("base_url") for r in outs[1]["results"]]
        self.assertEqual(urls, ["https://example.com/spring", "https://example.com/pricing"])

    def test_prepare_batch_accepts_a_bom_plan_and_hashes_the_same_requests(self):
        runner = ("import runpy, sys\nsys.path.insert(0, sys.argv[1])\nimport connector_resolver as cr\n"
                  "cr._load_mcp_json = lambda: {'slack': {}}\n"
                  "sys.argv = [sys.argv[1] + '/connector_executor.py'] + sys.argv[2:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        plan = json.dumps([{"action": "internal-kickoff", "data": {"plan": {
            "slack_channel": "#launch", "kickoff_message": "Launch is go", "kickoff_blocks": "none"}}}])
        hashes = []
        for prefix in (b"", BOM):
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "brands" / "acme").mkdir(parents=True)
                path = Path(tmp) / "plan.json"
                path.write_bytes(prefix + plan.encode("utf-8"))
                env = dict(os.environ, CLAUDE_MARKETING_HOME=tmp, HOME=tmp, USERPROFILE=tmp,
                           SLACK_BOT_TOKEN="xoxb-test-token-123", PYTHONIOENCODING="utf-8")
                code, out = run(["-c", runner, str(SCRIPTS_DIR), "--brand", "acme",
                                 "--prepare-batch", str(path)], env=env)
                self.assertEqual(code, 2, out)
                result = json.loads(out)
                self.assertEqual(result["status"], "prepared")
                hashes.append([item["payload_hash"] for item in result["items"]])
        self.assertEqual(hashes[0], hashes[1], "the hash comes from the parsed request, not the file's bytes")


class TestPrintedHintsRunFromAnyFolder(unittest.TestCase):
    HINT = re.compile(r"\bpython3?\s+(?:scripts/)?[\w-]+\.py")

    def runtime_hints(self):
        out = []
        for p in sorted(SCRIPTS_DIR.glob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
            skip = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body \
                        and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    skip.add(id(node.body[0].value))  # docstrings document usage; they are not runtime hints
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg in ("epilog", "description", "usage", "help"):
                            skip.update(id(sub) for sub in ast.walk(kw.value))
            out += [f"{p.name}:{n.lineno}: {n.value.strip()[:80]}" for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
                    and self.HINT.search(n.value)]
        return out

    def test_no_script_prints_a_relative_run_hint(self):
        self.assertEqual(self.runtime_hints(), [])

    def test_script_command_is_absolute_and_quoted(self):
        cmd = _common.script_command("refresh_models.py", "--check")
        path = cmd.split('"')[-2] if cmd.count('"') >= 2 else ""
        self.assertTrue(Path(path).is_absolute() and Path(path).exists(), cmd)
        self.assertTrue(cmd.endswith('refresh_models.py" --check'), cmd)


if __name__ == "__main__":
    unittest.main()
