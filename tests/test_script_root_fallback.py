"""Every skill or command that runs `${CLAUDE_PLUGIN_ROOT}/scripts/...` says where
the scripts are when the host does not set that variable.

Hermes review (NousResearch/hermes-agent#132571): Hermes does not define
`${CLAUDE_PLUGIN_ROOT}`, so `python ${CLAUDE_PLUGIN_ROOT}/scripts/x.py`
expands to `/scripts/x.py` and fails. The fix is one prose line, identical in
ContentForge and SocialForge, that stays correct on every host (Claude Code,
Codex, Cursor and the others set the variable; the line is then just a note).
It lives in skill bodies, not descriptions, so the listing budget is untouched.

2026-10-12 (fresh-install smoke test): the line also names the interpreter. Many macOS and
Linux systems have no `python` command, only `python3`, so a skill's `python ...` failed
there with no hint. The line now says to run the scripts with Python 3.10 or newer, as
`python` or `python3`. The commands themselves keep `python`.

Stdlib only.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = "${CLAUDE_PLUGIN_ROOT}"
FALLBACK = ("> **Script location.** If your host does not set `${CLAUDE_PLUGIN_ROOT}`, the scripts are in "
            "this plugin's `scripts/` folder, next to `skills/`. Run them with Python 3.10 or newer: "
            "`python`, or `python3` where `python` is not found (macOS, many Linux systems).")
# The 3.35.0-3.35.3 line, without the interpreter sentence: a file still carrying it is flagged.
OLD_FALLBACK = FALLBACK.split(" Run them with")[0]
# A command line that runs one of the plugin's scripts, by the variable or by a bare path.
RUNS_SCRIPT = re.compile(r"\bpython3?\s+\"?(?:\$\{CLAUDE_PLUGIN_ROOT\}/)?scripts/[\w./-]+\.py")
# A bare relative path only works when the shell happens to sit in the plugin root.
RELATIVE_RUN = re.compile(r"\bpython3?\s+\"?scripts/[\w./-]+\.py")
# An unquoted path breaks on a plugin folder with a space in it (C:\Users\Jane Doe\...).
UNQUOTED_PATH = re.compile(r'(?<!")\$\{CLAUDE_PLUGIN_ROOT\}/scripts/')


def runs_a_script(text: str) -> bool:
    body = text.replace(FALLBACK, "")
    return bool(RUNS_SCRIPT.search(body)) or VAR in body


def missing_fallback(text: str) -> bool:
    """True if the text runs a script (or uses the variable) but lacks the line."""
    return runs_a_script(text) and FALLBACK not in text


# Folders the model is not sent to for instructions, and contributor docs that are run from a
# clone (where a relative `scripts/` path is right).
EXCLUDED_DIRS = {"docs", "tests", "research", "evals", "hermes", "heroes", "node_modules"}
CONTRIBUTOR_DOCS = re.compile(r"^(README.*|CHANGELOG|CONTRIBUTING|TESTING-GUIDE|TESTING|SUBMISSION|UPGRADE-GUIDE)\.md$",
                              re.I)


def checked_files():
    """Discovery by exclusion: every .md in the plugin except the folders and contributor docs
    above, so a new folder is covered by default.

    2026-10-12: the 24 agents ran scripts with no line, because the guard listed skills and
    commands; 29 reference files ran benchmark_book.py by a bare `scripts/` path; and PRIVACY.md
    told users to return autopilot to propose-only with `python scripts/campaign-health-monitor.py`,
    which fails from a user's project. Each time the file sat outside a list of folders.
    """
    out = []
    for p in ROOT.rglob("*.md"):
        rel = p.relative_to(ROOT).parts
        if any(part.startswith(".") for part in rel[:-1]) or rel[0] in EXCLUDED_DIRS \
                or CONTRIBUTOR_DOCS.match(p.name):
            continue
        out.append(p)
    return sorted(out)


class TestScriptRootFallback(unittest.TestCase):
    def test_every_file_that_runs_a_script_has_the_fallback(self):
        bad = [str(p.relative_to(ROOT)) for p in checked_files()
               if missing_fallback(p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(bad, [], "files that run a script without the script-location line "
                                  "(see this module's docstring):\n  " + "\n  ".join(bad))

    def test_every_plugin_root_script_path_is_quoted(self):
        bad = ["%s:%d" % (p.relative_to(ROOT), i) for p in checked_files()
               for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
               if UNQUOTED_PATH.search(line)]
        self.assertEqual(bad, [], 'write "${CLAUDE_PLUGIN_ROOT}/scripts/..." in double quotes:\n  ' + "\n  ".join(bad))

    def test_plant_unquoted_paths_are_flagged(self):
        self.assertTrue(UNQUOTED_PATH.search("python ${CLAUDE_PLUGIN_ROOT}/scripts/dm-status.py --json"))
        self.assertTrue(UNQUOTED_PATH.search("- `${CLAUDE_PLUGIN_ROOT}/scripts/performance-monitor.py` — data"))
        self.assertIsNone(UNQUOTED_PATH.search('python "${CLAUDE_PLUGIN_ROOT}/scripts/dm-status.py" --json'))
        self.assertIsNone(UNQUOTED_PATH.search(FALLBACK), "the script-location line names the variable, not a path")

    def test_no_file_runs_a_script_by_a_relative_path(self):
        bad = ["%s: %s" % (p.relative_to(ROOT), m.group(0)) for p in checked_files()
               for m in RELATIVE_RUN.finditer(p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(bad, [], "use python \"${CLAUDE_PLUGIN_ROOT}/scripts/...\":\n  " + "\n  ".join(bad))

    def test_the_line_is_present_where_expected(self):
        users = [p for p in checked_files() if runs_a_script(p.read_text(encoding="utf-8", errors="replace"))]
        self.assertGreaterEqual(len(users), 150, "expected the ~160 script-running files to be found")
        agents = [p for p in users if p.parent.name == "agents"]
        self.assertEqual(len(agents), len(list((ROOT / "agents").glob("*.md"))), "every agent runs scripts")

    def test_scope_is_everything_but_the_exclusions(self):
        names = {p.relative_to(ROOT).as_posix() for p in checked_files()}
        for must in ("PRIVACY.md", "AGENTS.md", "CONNECTORS.md", "SECURITY.md", "skills/help/SKILL.md",
                     "agents/execution-coordinator.md", "commands/execute-action.md"):
            self.assertIn(must, names)
        for never in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "TESTING-GUIDE.md", "SUBMISSION.md",
                      "docs/getting-started.md", "evals/README.md"):
            self.assertNotIn(never, names)
    # ── plant checks ─────────────────────────────────────────────

    def test_plant_missing_line_is_caught(self):
        self.assertTrue(missing_fallback('# X\n\nRun `python "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"`.\n'))

    def test_plant_line_present_passes(self):
        self.assertFalse(missing_fallback('# X\n\n' + FALLBACK + '\n\nRun `python "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"`.\n'))

    def test_plant_old_line_without_the_interpreter_is_caught(self):
        self.assertTrue(missing_fallback('# X\n\n' + OLD_FALLBACK + '\n\nRun `python "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"`.\n'))

    def test_plant_file_without_the_variable_is_ignored(self):
        self.assertFalse(missing_fallback("# X\n\nNo scripts here.\n"))

    def test_plant_a_bare_relative_run_needs_the_line_and_is_flagged(self):
        text = "# Agent\n\nRun `python3 scripts/approval-manager.py --action list`.\n"
        self.assertTrue(missing_fallback(text), "running a script by any path needs the line")
        self.assertTrue(RELATIVE_RUN.search(text))
        self.assertIsNone(RELATIVE_RUN.search('Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/a.py"`.'))
        self.assertIsNone(RELATIVE_RUN.search("python -m pip install -r scripts/requirements.txt"),
                          "pip reading a requirements file is not running a script")


if __name__ == "__main__":
    unittest.main()
