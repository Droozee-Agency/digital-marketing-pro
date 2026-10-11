"""Write skills retry only a request that provably never reached the platform.

Writes through MCP server tools are outside connector_executor.py's approval-record check,
so the skill prose is their only guard against a duplicate. Eight write skills and guides
told the agent to "retry transient failures" or "retry delivery 2x", and a timeout, the case
data-import named, can come after the platform already created the contact, row, post or
email. The one rule now lives in skills/context-engine/approval-framework.md and, in short
form, on every line of these files that mentions a retry. Read-only retries (a data pull)
are untouched.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULE = ("Retry only a write that provably never reached the platform (connection refused, DNS failure, or a 429 "
        "rate-limit rejection). After a timeout or a lost reply, look the record up on the platform first (by email, "
        "external ID or message ID) and resend only what is missing.")
WRITE_FILES = [
    "skills/crm-sync/SKILL.md",
    "skills/data-import/SKILL.md",
    "skills/lead-import/SKILL.md",
    "skills/data-export/SKILL.md",
    "skills/send-report/SKILL.md",
    "skills/schedule-social/SKILL.md",
    "skills/client-report/SKILL.md",
    "skills/context-engine/advanced-reporting-guide.md",
    "skills/context-engine/approval-framework.md",
]
RETRY = re.compile(r"\bretr(?:y|ies|ied|ying)\b", re.I)
QUALIFIED = re.compile(r"provably never|never auto-retry|never retry", re.I)
# Read retries that stay as they are: re-pulling data creates nothing on a platform.
READ_ONLY = {"skills/context-engine/advanced-reporting-guide.md": ("during data pull",)}


def unqualified(rel: str, text: str) -> list:
    exempt = READ_ONLY.get(rel, ())
    return [line.strip()[:120] for line in text.splitlines()
            if RETRY.search(line) and not QUALIFIED.search(line) and not any(e in line for e in exempt)]


class TestWriteRetryRule(unittest.TestCase):
    def test_the_rule_is_worded_once_in_the_approval_framework(self):
        text = (ROOT / "skills/context-engine/approval-framework.md").read_text(encoding="utf-8")
        self.assertIn(RULE, text)

    def test_every_retry_line_in_the_write_skills_carries_the_rule(self):
        bad = []
        for rel in WRITE_FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertTrue(RETRY.search(text), f"{rel} no longer mentions a retry; drop it from WRITE_FILES")
            bad += [f"{rel}: {line}" for line in unqualified(rel, text)]
        self.assertEqual(bad, [], "a write-skill line mentions a retry without the provably-never rule:\n  "
                         + "\n  ".join(bad))

    def test_the_read_retry_is_left_alone(self):
        text = (ROOT / "skills/context-engine/advanced-reporting-guide.md").read_text(encoding="utf-8")
        self.assertIn("Retry 3x with exponential backoff", text)

    def test_plant_the_old_crm_sync_line_is_flagged(self):
        old = ("9. **On approval, execute sync via CRM MCP**: Handle rate limits with exponential backoff, "
               "retry transient failures up to 3 times, and log each record's outcome.")
        self.assertEqual(len(unqualified("skills/crm-sync/SKILL.md", old)), 1)
        self.assertEqual(unqualified("skills/crm-sync/SKILL.md", "Never auto-retry a failed execution."), [])
        self.assertEqual(unqualified("skills/context-engine/advanced-reporting-guide.md",
                                     "| API timeout or error response during data pull | Retry 3x |"), [])


if __name__ == "__main__":
    unittest.main()
