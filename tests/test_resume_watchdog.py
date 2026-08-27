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

from auto_resume.repo import fingerprint, repo_matches
from auto_resume.resume import ResumeError, resume_thread
from auto_resume.watch import decide_action
from auto_resume.watch import run_job
from auto_resume.state import load_json, save_job


class ResumeWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
        self.command = [sys.executable, str(self.fake)]
        self.thread_id = str(uuid.uuid4())

    def test_resume_uses_exact_uuid_and_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "argv.json"
            env = {"FAKE_ARGV_LOG": str(log), "FAKE_THREAD_ID": self.thread_id}
            result = resume_thread(self.command, self.thread_id, "PROMPT", Path(tmp), env=env)
            self.assertTrue(result.completed)
            self.assertEqual(["exec", "resume", self.thread_id, "PROMPT", "--json"], json.loads(log.read_text()))

    def test_resume_rejects_mismatched_started_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ResumeError):
                resume_thread(self.command, self.thread_id, "PROMPT", Path(tmp),
                              env={"FAKE_THREAD_ID": str(uuid.uuid4())})

    def test_invalid_uuid_is_rejected_before_process_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                resume_thread(["missing-program"], "not-a-uuid", "PROMPT", Path(tmp))

    def test_repo_fingerprint_detects_visible_untracked_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
            Path(tmp, "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            before = fingerprint(Path(tmp))
            Path(tmp, "new.txt").write_text("new", encoding="utf-8")
            self.assertFalse(repo_matches(Path(tmp), before))

    def test_decisions_cover_done_max_cycles_conflict_and_resume(self):
        base = {"status": "RUNNING", "completed_cycles": 0, "max_cycles": 5}
        self.assertEqual("done", decide_action({**base, "status": "DONE"}, True, False))
        self.assertEqual("max_cycles", decide_action({**base, "completed_cycles": 5}, True, False))
        self.assertEqual("needs_user", decide_action(base, False, False))
        self.assertEqual("wait", decide_action(base, True, True))
        self.assertEqual("resume", decide_action(base, True, False))

    @unittest.skipUnless(os.name == "nt", "Windows-only creation flags")
    def test_windows_detach_flags_are_present(self):
        from auto_resume.registering import windows_creation_flags
        flags = windows_creation_flags()
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)

    def test_exhausted_window_withholds_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            Path(project, "a").write_text("a")
            subprocess.run(["git", "add", "a"], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
            checkpoint = Path(tmp) / "checkpoint.md"
            checkpoint.write_text("## AUTO_RESUME_STATUS\nRUNNING\n", encoding="utf-8")
            job = self._job(project, checkpoint, "RUNNING")
            job_path = Path(tmp) / "job.json"
            save_job(job_path, job)
            argv_log = Path(tmp) / "argv.json"
            future = __import__("time").time() + 3600
            env = {"FAKE_LIMITS": json.dumps({"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": future}}}),
                   "FAKE_ARGV_LOG": str(argv_log)}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual("WAITING_RESET", run_job(job_path, self.command, once=True))
            self.assertFalse(argv_log.exists())

    def test_fake_end_to_end_resumes_after_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            Path(project, "a").write_text("a")
            subprocess.run(["git", "add", "a"], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
            checkpoint = Path(tmp) / "checkpoint.md"
            checkpoint.write_text("## AUTO_RESUME_STATUS\nRUNNING\n", encoding="utf-8")
            job = self._job(project, checkpoint, "WAITING_RESET")
            job_path = Path(tmp) / "job.json"
            save_job(job_path, job)
            env = {"FAKE_LIMITS": json.dumps({"rateLimits": {"primary": {"usedPercent": 0, "resetsAt": None}}}),
                   "FAKE_THREAD_ID": self.thread_id}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("auto_resume.watch._settled", return_value=job["expected_repo_snapshot"]):
                self.assertEqual("RUNNING", run_job(job_path, self.command, once=True))
            updated = load_json(job_path)
            self.assertEqual(1, updated["completed_cycles"])
            self.assertEqual("RUNNING", updated["status"])

    def _job(self, project, checkpoint, status):
        now = "2026-01-01T00:00:00+00:00"
        return {"schema_version": 1, "job_id": "job", "thread_id": self.thread_id,
                "project_root": str(project), "original_goal": "goal", "status": status,
                "billing_policy": "included_only", "limit_id": "codex", "max_cycles": 5,
                "completed_cycles": 0, "poll_interval_seconds": 1, "safety_margin_seconds": 0,
                "checkpoint_path": str(checkpoint), "expected_repo_snapshot": fingerprint(project),
                "watchdog_pid": None, "created_at": now, "updated_at": now, "last_error": None}


if __name__ == "__main__":
    unittest.main()
