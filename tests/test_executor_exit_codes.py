"""connector_executor.py's exit status says whether anything was sent.

A fresh-install smoke test (2026-10-12) found `--execute` exiting 0 when nothing was
sent because no connector was configured or the credential variable was missing, so
a caller chaining on `$?` read a non-action as success. Now: 0 sent (or a local action,
or a dry run), 1 error or the service reported failure, 2 an approval record is needed,
3 --execute given but nothing could be sent. The JSON still says why.

The requests are the real Slack and GA4 actions built through the real resolver; only
the network (urlopen) and the MCP config are mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SCRIPTS_DIR, import_script  # noqa: E402

sys.path.insert(0, str(SCRIPTS_DIR))
import connector_executor as ce  # noqa: E402
import connector_resolver as cr  # noqa: E402

BRAND = "acme"
TOKEN = "xoxb-test-token-123"
PLAN = {"plan": {"slack_channel": "#launch", "kickoff_message": "Launch is go", "kickoff_blocks": "none"}}


class _Resp:
    def __init__(self, ok=True):
        self.status = 200
        self._ok = ok

    def read(self):
        return json.dumps({"ok": self._ok, "ts": "1.2"}).encode()

    def close(self):
        pass


class ExitCodeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "brands" / BRAND).mkdir(parents=True)
        mock.patch.dict(os.environ, {"CLAUDE_MARKETING_HOME": str(self.home)}).start()
        mock.patch.object(cr, "BRANDS_DIR", self.home / "brands").start()
        self.mcp = mock.patch.object(cr, "_load_mcp_json", return_value={"slack": {}}).start()
        self.urlopen = mock.patch.object(ce.urllib.request, "urlopen", return_value=_Resp()).start()
        self.addCleanup(mock.patch.stopall)

    def run_exec(self, env, action="internal-kickoff", execute=True, **kw):
        result = ce.execute_action(action, BRAND, execute=execute, env=env, data=PLAN,
                                   log_to_tracker=False, **kw)
        return result, ce.exit_code_for(result, execute)

    def approvals(self):
        d = self.home / "brands" / BRAND / "approvals"
        return sorted(d.glob("*.json")) if d.exists() else []

    def test_no_connector_configured_exits_3(self):
        self.mcp.return_value = {}
        result, code = self.run_exec({})
        self.assertEqual(code, ce.NOT_EXECUTED_EXIT)
        self.assertFalse(result["execute_attempted"])
        self.assertIn("nothing to execute", result["execute_blocked_reason"])

    def test_missing_credential_exits_3_and_writes_no_record(self):
        result, code = self.run_exec({})
        self.assertEqual(code, ce.NOT_EXECUTED_EXIT)
        self.assertIn("SLACK_BOT_TOKEN is not set", result["execute_blocked_reason"])
        self.assertIn("setup_hint_credential", result)
        self.assertEqual(self.approvals(), [], "no record is prepared for a request that cannot fire")
        self.urlopen.assert_not_called()

    def test_mcp_only_connector_exits_3(self):
        self.mcp.return_value = {"meta-ads": {}}
        result, code = self.run_exec({}, action="launch-ads")
        self.assertEqual(code, ce.NOT_EXECUTED_EXIT)
        self.assertIn("MCP", result["alternative"])
        self.urlopen.assert_not_called()

    def test_pending_record_exits_2(self):
        result, code = self.run_exec({"SLACK_BOT_TOKEN": TOKEN})
        self.assertEqual(code, ce.APPROVAL_BLOCKED_EXIT)
        self.assertTrue(result["approval_required"])
        self.assertEqual(len(self.approvals()), 1)
        self.urlopen.assert_not_called()

    def test_refused_approval_id_exits_2(self):
        result, code = self.run_exec({"SLACK_BOT_TOKEN": TOKEN}, approval_id="exec-does-not-exist")
        self.assertEqual(code, ce.APPROVAL_BLOCKED_EXIT)
        self.urlopen.assert_not_called()

    def test_sent_and_succeeded_exits_0_sent_and_failed_exits_1(self):
        am = import_script("approval-manager.py", "approval_manager_exit_test")
        env = {"SLACK_BOT_TOKEN": TOKEN}
        for ok, expected in ((True, 0), (False, 1)):
            with self.subTest(ok=ok):
                self.urlopen.return_value = _Resp(ok=ok)
                prep, _ = self.run_exec(env)
                am.approve(BRAND, prep["approval_id"])
                result, code = self.run_exec(env, approval_id=prep["approval_id"])
                self.assertTrue(result["execute_attempted"])
                self.assertEqual(code, expected, result.get("execution"))

    def test_dry_run_exits_0(self):
        result, code = self.run_exec({}, execute=False)
        self.assertEqual(code, 0)
        self.assertNotIn("execute_attempted", result)

    def test_error_result_exits_1(self):
        self.assertEqual(ce.exit_code_for({"error": "unknown action"}, True), 1)
        self.assertEqual(ce.exit_code_for(None, True), 1)


RUNNER = (
    "import json, runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import connector_resolver as cr\n"
    "cr._load_mcp_json = lambda: {'slack': {}}\n"
    "sys.argv = [sys.argv[1] + '/connector_executor.py'] + sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


class TestCliExitStatus(unittest.TestCase):
    """The status a shell sees, from the real CLI (only the MCP config is stubbed)."""

    def run_cli(self, token):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "brands" / BRAND).mkdir(parents=True)
            env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
            env.update({"CLAUDE_MARKETING_HOME": tmp, "HOME": tmp, "USERPROFILE": tmp,
                        "PYTHONIOENCODING": "utf-8"})
            if token:
                env["SLACK_BOT_TOKEN"] = token
            proc = subprocess.run([sys.executable, "-c", RUNNER, str(SCRIPTS_DIR),
                                   "--action", "internal-kickoff", "--brand", BRAND, "--execute",
                                   "--data", json.dumps(PLAN)],
                                  capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        return proc.returncode, json.loads(proc.stdout)

    def test_missing_credential_is_not_success(self):
        code, out = self.run_cli(token=None)
        self.assertEqual(code, 3, out)
        self.assertFalse(out["execute_attempted"])

    def test_pending_record_is_2(self):
        code, out = self.run_cli(token=TOKEN)
        self.assertEqual(code, 2, out)
        self.assertNotIn(TOKEN, json.dumps(out), "the credential never appears in the output")


if __name__ == "__main__":
    unittest.main()
