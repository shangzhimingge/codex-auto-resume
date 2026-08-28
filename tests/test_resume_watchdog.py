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
from auto_resume.resume import ResumeError, ResumeInterrupted, resume_thread
from auto_resume.watch import decide_action, _next_wait
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

    def test_uppercase_uuid_is_rejected_without_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                resume_thread(["missing-program"], self.thread_id.upper(), "PROMPT", Path(tmp))

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

    def test_repo_fingerprint_hashes_dotfile_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
            hidden = Path(tmp, ".github", "workflow.yml")
            hidden.parent.mkdir()
            Path(tmp, "seed.txt").write_text("seed", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            hidden.write_text("one", encoding="utf-8")
            before = fingerprint(Path(tmp))
            hidden.write_text("two", encoding="utf-8")
            self.assertFalse(repo_matches(Path(tmp), before))

    def test_repo_fingerprint_hashes_renamed_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
            Path(tmp, "old.txt").write_text("original", encoding="utf-8")
            subprocess.run(["git", "add", "old.txt"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            subprocess.run(["git", "mv", "old.txt", "new.txt"], cwd=tmp, check=True)
            before = fingerprint(Path(tmp))
            Path(tmp, "new.txt").write_text("changed", encoding="utf-8")
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

    def test_reset_sleep_is_capped_and_handles_short_or_past_deadline(self):
        self.assertEqual(10, _next_wait(deadline=1000, now=100, poll_interval=10, safety_margin=30))
        self.assertEqual(7, _next_wait(deadline=105, now=100, poll_interval=10, safety_margin=2))
        self.assertEqual(1, _next_wait(deadline=90, now=100, poll_interval=10, safety_margin=30))

    def test_waiting_reset_re_reads_a_changed_reset_timestamp_each_poll(self):
        from auto_resume.limits import LimitsSnapshot
        class StopPolling(Exception):
            pass
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
            job["poll_interval_seconds"] = 10
            job_path = Path(tmp) / "job.json"
            save_job(job_path, job)
            snapshots = [
                LimitsSnapshot("codex", [{"name":"primary","used_percent":100,"resets_at":1000}], {}),
                LimitsSnapshot("codex", [{"name":"primary","used_percent":100,"resets_at":2000}], {}),
            ]
            sleeps = []
            def record_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 2:
                    raise StopPolling()
            with mock.patch("auto_resume.watch.read_limits", side_effect=snapshots):
                with self.assertRaises(StopPolling):
                    run_job(job_path, self.command, sleep=record_sleep, now=lambda: 100)
            self.assertEqual([10, 10], sleeps)

    def test_supervised_resume_stops_when_guard_detects_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FAKE_THREAD_ID": self.thread_id, "FAKE_RESUME_SLEEP": "10"}
            calls = []
            def guard():
                calls.append(True)
                return "limit_exhausted" if len(calls) >= 2 else None
            started = __import__("time").monotonic()
            with self.assertRaises(ResumeInterrupted) as caught:
                resume_thread(self.command, self.thread_id, "PROMPT", Path(tmp), env=env,
                              supervisor=guard, supervisor_interval=0.05)
            self.assertEqual("limit_exhausted", caught.exception.reason)
            self.assertTrue(caught.exception.thread_verified)
            self.assertLess(__import__("time").monotonic() - started, 3)

    def test_watchdog_returns_to_waiting_when_resume_hits_included_limit(self):
        from auto_resume.limits import LimitsSnapshot
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
            available = LimitsSnapshot("codex", [{"name":"primary","used_percent":0,"resets_at":None}], {}, None)
            interrupted = ResumeInterrupted("limit_exhausted", thread_verified=True)
            with mock.patch("auto_resume.watch.read_limits", return_value=available), \
                 mock.patch("auto_resume.watch._settled", return_value=job["expected_repo_snapshot"]), \
                 mock.patch("auto_resume.watch.resume_thread", side_effect=interrupted):
                self.assertEqual("WAITING_RESET", run_job(job_path, self.command, once=True))
            updated = load_json(job_path)
            self.assertEqual(1, updated["completed_cycles"])
            self.assertEqual("WAITING_RESET", updated["status"])

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
