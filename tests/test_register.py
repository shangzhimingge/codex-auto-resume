import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.registering import register_job, start_watchdog
from auto_resume.state import ACTIVE_STATES, REQUIRED_JOB_FIELDS, load_json


class RegisterTests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def test_register_creates_complete_job_checkpoint_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            home = Path(tmp) / "home"
            project.mkdir()
            self.make_repo(project)
            thread_id = str(uuid.uuid4())
            first = register_job(thread_id, project, "目标", home, start_watchdog=False)
            second = register_job(thread_id, project, "目标", home, start_watchdog=False)
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(set(REQUIRED_JOB_FIELDS), set(first))
            self.assertEqual("included_only", first["billing_policy"])
            self.assertIn(first["status"], ACTIVE_STATES)
            self.assertTrue(Path(first["checkpoint_path"]).exists())

    def test_register_rejects_non_git_and_invalid_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                register_job("bad", Path(tmp), "目标", Path(tmp) / "home", start_watchdog=False)

    def test_start_watchdog_detaches_run_command_and_saves_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "job.json"
            project = Path(tmp) / "project"
            project.mkdir()
            now = "2026-01-01T00:00:00+00:00"
            job = {key: None for key in REQUIRED_JOB_FIELDS}
            job.update({"schema_version": 1, "job_id": "job", "thread_id": str(uuid.uuid4()),
                        "project_root": str(project), "original_goal": "goal", "status": "REGISTERED",
                        "billing_policy": "included_only", "limit_id": "codex", "max_cycles": 5,
                        "completed_cycles": 0, "poll_interval_seconds": 60, "safety_margin_seconds": 30,
                        "checkpoint_path": str(Path(tmp) / "c.md"), "expected_repo_snapshot": {},
                        "created_at": now, "updated_at": now, "last_error": None})
            from auto_resume.state import save_job
            save_job(job_path, job)
            fake_process = mock.Mock(pid=1234)
            with mock.patch("auto_resume.registering.subprocess.Popen", return_value=fake_process) as popen:
                self.assertEqual(1234, start_watchdog(job_path))
            argv = popen.call_args.args[0]
            self.assertEqual("run", argv[2])
            self.assertEqual("--job", argv[3])
            self.assertEqual(1234, load_json(job_path)["watchdog_pid"])


if __name__ == "__main__":
    unittest.main()
