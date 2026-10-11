"""An approval record is released only when nothing can have reached the platform.

A fresh-install smoke test (2026-10-12) reproduced a double send: connector_executor
treated any failure without an HTTP status as "nothing sent" and released the record,
but a timeout after the request was delivered (or after "200 OK" arrived) also has no
status, so one approval could fire twice after a slow response. Now only errors that
prove no connection was made (refused, DNS, proxy tunnel) release it; everything else
is consumed with outcome "unknown".

A ~25-line stub server on 127.0.0.1 plays the slow platform. No network.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import SCRIPTS_DIR, import_script  # noqa: E402

sys.path.insert(0, str(SCRIPTS_DIR))
import connector_executor as ce  # noqa: E402
import connector_resolver as cr  # noqa: E402

CLIENT_TIMEOUT = 1
STALL = 2.5
# On Windows a refused localhost port can take ~2 s and, under a short timeout, surface
# as "timed out" (consumed, the safe reading); a long timeout lets the refusal itself arrive.
REFUSAL_TIMEOUT = 10


class StubPlatform:
    """Accepts one connection per request. mode: 'ok', 'stall_before_status', 'stall_after_status'."""

    def __init__(self, mode):
        self.mode, self.bodies = mode, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, body = data.split(b"\r\n\r\n", 1)
                length = int(next((ln.split(b":")[1] for ln in head.split(b"\r\n")
                                   if ln.lower().startswith(b"content-length")), b"0"))
                while len(body) < length:
                    body += conn.recv(65536)
                self.bodies.append(body)            # the platform has the whole request
                if self.mode == "ok":
                    out = b'{"ok": true}'
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                 + str(len(out)).encode() + b"\r\n\r\n" + out)
                    continue
                if self.mode == "stall_after_status":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: 100\r\n\r\n{\"ok\": tr")
                time.sleep(STALL)

    def close(self):
        self.sock.close()


def refused_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def built(port, body=b'{"text": "Launch is go"}'):
    return {"method": "POST", "url": f"http://127.0.0.1:{port}/api/chat.postMessage",
            "headers": {"Content-Type": "application/json"}, "body_bytes": body}


class NoProxy(unittest.TestCase):
    def setUp(self):
        # no_proxy=* keeps urllib off any system or registry proxy for 127.0.0.1.
        mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}).start()
        for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(key, None)
        self.addCleanup(mock.patch.stopall)


class TestSendRequestOutcomes(NoProxy):
    def stub(self, mode):
        s = StubPlatform(mode)
        self.addCleanup(s.close)
        return s

    def test_stall_before_status_is_sent_with_unknown_outcome(self):
        s = self.stub("stall_before_status")
        r = ce.send_request(built(s.port), timeout=CLIENT_TIMEOUT)
        self.assertEqual(s.bodies, [b'{"text": "Launch is go"}'], "the platform received the request")
        self.assertEqual(r["status"], "unknown_outcome")
        self.assertIs(r["sent"], True)
        self.assertNotIn("http_status", r)
        self.assertIn("check it there", r["outcome"])

    def test_stall_after_status_keeps_the_status(self):
        s = self.stub("stall_after_status")
        r = ce.send_request(built(s.port), timeout=CLIENT_TIMEOUT)
        self.assertEqual(r["status"], "unknown_outcome")
        self.assertIs(r["sent"], True)
        self.assertEqual(r["http_status"], 200)

    def test_refused_connection_proves_nothing_was_sent(self):
        r = ce.send_request(built(refused_port()), timeout=REFUSAL_TIMEOUT)
        self.assertEqual(r["status"], "network_error", r)
        self.assertIs(r["sent"], False)

    def test_a_timeout_proves_nothing_and_is_consumed(self):
        for exc in (ce.urllib.error.URLError(socket.timeout("timed out")), TimeoutError("timed out"),
                    ConnectionResetError(10054, "reset")):
            with self.subTest(exc=repr(exc)), mock.patch.object(ce.urllib.request, "urlopen", side_effect=exc):
                r = ce.send_request(built(80), timeout=CLIENT_TIMEOUT)
                self.assertEqual((r["status"], r["sent"]), ("unknown_outcome", True))

    def test_errno_proven_refusal_releases(self):
        for exc in (ce.urllib.error.URLError(ConnectionRefusedError(111, "refused")),
                    ce.urllib.error.URLError(OSError(ce.errno.ECONNREFUSED, "refused")),
                    ce.urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                    ce.urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))):
            with self.subTest(exc=repr(exc)), mock.patch.object(ce.urllib.request, "urlopen", side_effect=exc):
                r = ce.send_request(built(80), timeout=CLIENT_TIMEOUT)
                self.assertEqual((r["status"], r["sent"]), ("network_error", False))

    def test_unresolvable_host_proves_nothing_was_sent(self):
        b = built(80)
        b["url"] = "http://no-such-host.invalid/api"
        r = ce.send_request(b, timeout=CLIENT_TIMEOUT)
        self.assertIs(r["sent"], False, r)

    def test_a_completed_request_is_executed(self):
        s = self.stub("ok")
        r = ce.send_request(built(s.port), timeout=CLIENT_TIMEOUT)
        self.assertEqual((r["status"], r["http_status"], r["sent"]), ("executed", 200, True))

    def test_incomplete_outcomes_are_never_success(self):
        for r in ({"status": "unknown_outcome", "sent": True, "http_status": 200},
                  {"status": "network_error", "sent": False}):
            self.assertIs(ce._check_success(dict(r), {"success_codes": [200]})["success"], False, r)


class TestOneApprovalFiresOnce(NoProxy):
    """The real Slack internal-kickoff write, pointed at the stub; approve once, fire twice."""

    PLAN = {"plan": {"slack_channel": "#launch", "kickoff_message": "Launch is go", "kickoff_blocks": "none"}}
    ENV = {"SLACK_BOT_TOKEN": "xoxb-test-token-123"}

    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / "brands" / "acme").mkdir(parents=True)
        mock.patch.dict(os.environ, {"CLAUDE_MARKETING_HOME": str(self.home)}).start()
        mock.patch.object(cr, "BRANDS_DIR", self.home / "brands").start()
        mock.patch.object(cr, "_load_mcp_json", return_value={"slack": {}}).start()
        self.am = import_script("approval-manager.py", "approval_manager_delivery_test")

    def point_at(self, port):
        real = ce._build_for_action

        def to_stub(*args, **kwargs):
            out = real(*args, **kwargs)
            if out.get("status") == "built":
                out["url"] = f"http://127.0.0.1:{port}/api/chat.postMessage"
            return out
        mock.patch.object(ce, "_build_for_action", side_effect=to_stub).start()

    def fire(self, approval_id=None, timeout=CLIENT_TIMEOUT):
        return ce.execute_action("internal-kickoff", "acme", execute=True, env=dict(self.ENV), data=self.PLAN,
                                 approval_id=approval_id, log_to_tracker=False, timeout=timeout)

    def record(self, approval_id):
        return json.loads(next((self.home / "brands" / "acme" / "approvals").glob(f"{approval_id}*.json"))
                          .read_text(encoding="utf-8"))

    def approved_id(self):
        prep = self.fire()
        self.am.approve("acme", prep["approval_id"])
        return prep["approval_id"]

    def test_a_timeout_after_delivery_consumes_the_record(self):
        for mode in ("stall_before_status", "stall_after_status"):
            with self.subTest(mode=mode):
                stub = StubPlatform(mode)
                self.addCleanup(stub.close)
                self.point_at(stub.port)
                aid = self.approved_id()
                first = self.fire(aid)
                self.assertEqual(first["execution"]["status"], "unknown_outcome")
                self.assertIs(first["approval"]["consumed"], True)
                self.assertEqual(ce.exit_code_for(first, True), 1, "an unknown outcome is not success")
                rec = self.record(aid)
                self.assertEqual(rec["execution_result"], "unknown")
                second = self.fire(aid)
                self.assertFalse(second["execute_attempted"], "the same approval must not fire again")
                self.assertIn("already used", second["execute_blocked_reason"])
                self.assertEqual(len(stub.bodies), 1, "the platform saw exactly one request")

    def test_a_refused_connection_releases_the_record(self):
        self.point_at(refused_port())
        aid = self.approved_id()
        first = self.fire(aid, timeout=REFUSAL_TIMEOUT)
        self.assertEqual(first["execution"]["status"], "network_error", first["execution"])
        self.assertIs(first["approval"]["consumed"], False)
        self.assertEqual(self.record(aid)["status"], "approved", "released: nothing reached the platform")
        self.assertEqual(ce.exit_code_for(first, True), 1)


if __name__ == "__main__":
    unittest.main()
