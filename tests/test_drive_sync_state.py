"""Cowork+Drive sync state ledger (scripts/drive-sync-state.py).

Uses a temp HOME so we never touch the real ~/.claude-marketing/."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import import_script  # type: ignore[import-not-found]


class DriveSyncStateBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # The workspace resolver reads CLAUDE_MARKETING_HOME and CLAUDE_PLUGIN_DATA before
        # HOME, so a value set by whoever runs the suite pointed these tests at a shared
        # folder and they failed. Pin every input; patch.dict restores the environment.
        env = mock.patch.dict(os.environ, {
            "HOME": self.tmpdir.name,
            "USERPROFILE": self.tmpdir.name,
            "CLAUDE_MARKETING_HOME": str(Path(self.tmpdir.name) / ".claude-marketing"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        # Re-import after env change so Path.home() picks up the temp dir.
        mod_name = "drive_sync_state"
        sys.modules.pop(mod_name, None)
        self.dss = import_script("drive-sync-state.py", module_name=mod_name)


class TestCoworkConfig(DriveSyncStateBase):
    def test_read_config_when_absent(self):
        result = self.dss.read_cowork_config()
        self.assertFalse(result["configured"])
        self.assertIn("note", result)

    def test_write_then_read_config(self):
        payload = {
            "environment": "cowork-sandbox",
            "drive_root_folder_name": "TestRoot",
            "drive_root_folder_id": "1abcXYZ",
            "drive_root_folder_url": "https://drive.google.com/drive/folders/1abcXYZ",
            "drive_mcp_tool_prefix": "mcp__test__",
        }
        write_result = self.dss.write_cowork_config(payload)
        self.assertEqual(write_result["status"], "written")
        self.assertIn("configured_at", write_result["config"])

        read_result = self.dss.read_cowork_config()
        self.assertTrue(read_result["configured"])
        self.assertEqual(read_result["drive_root_folder_name"], "TestRoot")


class TestBrandProfileSync(DriveSyncStateBase):
    def _make_profile(self, brand: str, content: str = "name: Acme"):
        path = self.dss._profile_path(brand)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_no_profile_no_upload(self):
        result = self.dss.profile_needs_upload("acme")
        self.assertFalse(result["needs_upload"])
        self.assertIn("no local profile.json", result["reason"])

    def test_new_profile_needs_upload(self):
        self._make_profile("acme")
        result = self.dss.profile_needs_upload("acme")
        self.assertTrue(result["needs_upload"])
        self.assertEqual(result["reason"], "never uploaded to Drive")
        self.assertTrue(result["local_hash"].startswith("sha256:"))

    def test_marking_uploaded_makes_in_sync(self):
        self._make_profile("acme", content="initial")
        self.dss.profile_mark_uploaded("acme", "drive_id_1", "https://drive/x")
        result = self.dss.profile_needs_upload("acme")
        self.assertFalse(result["needs_upload"])
        self.assertEqual(result["reason"], "local matches last uploaded")

    def test_local_change_after_upload_triggers_resync(self):
        self._make_profile("acme", content="v1")
        self.dss.profile_mark_uploaded("acme", "drive_id_1", "https://drive/x")
        self._make_profile("acme", content="v2 - edited locally")
        result = self.dss.profile_needs_upload("acme")
        self.assertTrue(result["needs_upload"])
        self.assertEqual(result["reason"], "local content differs from last uploaded")

    def test_profile_drive_state_fields(self):
        self._make_profile("acme")
        self.dss.profile_mark_uploaded("acme", "drive_id_99", "https://drive/y")
        state = self.dss.profile_drive_state("acme")
        self.assertTrue(state["local_profile_exists"])
        self.assertTrue(state["in_sync"])
        self.assertEqual(state["drive_file_id"], "drive_id_99")


class TestRunCheckpointSync(DriveSyncStateBase):
    def test_add_then_list_pending(self):
        self.dss.add_pending_upload("acme", "20260608-001", "00-input.md")
        self.dss.add_pending_upload("acme", "20260608-001", "01-research.md")
        result = self.dss.list_pending_uploads("acme", "20260608-001")
        self.assertEqual(len(result["pending"]), 2)
        self.assertEqual(len(result["uploaded"]), 0)

    def test_add_same_file_twice_is_idempotent(self):
        self.dss.add_pending_upload("acme", "r1", "same.md")
        self.dss.add_pending_upload("acme", "r1", "same.md")
        result = self.dss.list_pending_uploads("acme", "r1")
        self.assertEqual(len(result["pending"]), 1)

    def test_mark_uploaded_moves_file(self):
        self.dss.add_pending_upload("acme", "r1", "00-input.md")
        self.dss.add_pending_upload("acme", "r1", "01-plan.md")
        self.dss.mark_run_file_uploaded("acme", "r1", "00-input.md", "drv_001")
        result = self.dss.list_pending_uploads("acme", "r1")
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(len(result["uploaded"]), 1)
        self.assertEqual(result["uploaded"][0]["file"], "00-input.md")
        self.assertEqual(result["uploaded"][0]["drive_file_id"], "drv_001")

    def test_list_runs_needing_sync(self):
        self.dss.add_pending_upload("acme", "r1", "f1.md")
        self.dss.add_pending_upload("acme", "r2", "f2.md")
        self.dss.mark_run_file_uploaded("acme", "r2", "f2.md", "drv_x")
        result = self.dss.list_runs_needing_sync("acme")
        run_ids = [r["run_id"] for r in result["runs"]]
        self.assertIn("r1", run_ids)
        self.assertNotIn("r2", run_ids)  # r2 has 0 pending


class TestSlugify(DriveSyncStateBase):
    # drive-sync-state.py delegates slugification to the ONE shared slugifier
    # (_common.slugify_brand); the local _slugify wrapper was removed.
    def test_slugify_handles_spaces_and_case(self):
        self.assertEqual(self.dss._common.slugify_brand("Acme Corp"), "acme-corp")

    def test_slugify_handles_special_chars(self):
        self.assertEqual(self.dss._common.slugify_brand("Acme/Corp v2!"), "acme-corp-v2")

    def test_slugify_strips_trailing_hyphens(self):
        self.assertEqual(self.dss._common.slugify_brand("---trim---"), "trim")


if __name__ == "__main__":
    unittest.main()
