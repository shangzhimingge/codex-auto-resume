import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.activation import preflight
from auto_resume.registering import register_job
from auto_resume.state import load_job, save_job
from auto_resume.watch import decide_action


class V110Tests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def test_v1_default_five_migrates_atomically_to_unlimited_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            job = self.job(max_cycles=5, schema_version=1)
            save_job(path, job, migrate=False)
            loaded = load_job(path)
            self.assertEqual(2, loaded["schema_version"])
            self.assertIsNone(loaded["max_cycles"])
            self.assertEqual(loaded, json.loads(path.read_text(encoding="utf-8")))

    def test_v1_custom_positive_limit_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            save_job(path, self.job(max_cycles=3, schema_version=1), migrate=False)
            self.assertEqual(3, load_job(path)["max_cycles"])

    def test_malformed_or_nonpositive_cycle_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (0, -1, "five"):
                path = Path(tmp) / f"{str(value).replace('-', 'n')}.json"
                path.write_text(json.dumps(self.job(max_cycles=value, schema_version=2)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_job(path)

    def test_default_registration_is_unlimited_and_finite_override_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            unlimited = register_job(str(uuid.uuid4()), project, "goal", home, start_watchdog=False)
            self.assertEqual(2, unlimited["schema_version"])
            self.assertIsNone(unlimited["max_cycles"])
            finite = register_job(str(uuid.uuid4()), project, "goal", home, max_cycles=7, start_watchdog=False)
            self.assertEqual(7, finite["max_cycles"])
            for bad in (0, -1):
                with self.assertRaises(ValueError):
                    register_job(str(uuid.uuid4()), project, "goal", home, max_cycles=bad, start_watchdog=False)

    def test_unlimited_never_selects_max_cycles(self):
        job = {"status": "RUNNING", "completed_cycles": 10_000, "max_cycles": None}
        self.assertEqual("resume", decide_action(job, True, False))

    def test_deduplicates_goal_wording_and_reuses_terminal_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            thread_id = str(uuid.uuid4())
            first = register_job(thread_id, project, "first wording", home, start_watchdog=False)
            second = register_job(thread_id, project, "different wording", home, start_watchdog=False)
            self.assertEqual(first["job_id"], second["job_id"])
            job_path = home / "auto-resume" / "jobs" / f"{first['job_id']}.json"
            first["status"] = "DONE"
            save_job(job_path, first)
            terminal = register_job(thread_id, project, "third wording", home, start_watchdog=False)
            self.assertEqual("DONE", terminal["status"])
            self.assertEqual(first["job_id"], terminal["job_id"])

    def test_active_watchdog_reused_or_stale_pid_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            thread_id = str(uuid.uuid4())
            first = register_job(thread_id, project, "goal", home, start_watchdog=False)
            job_path = home / "auto-resume" / "jobs" / f"{first['job_id']}.json"
            first["watchdog_pid"] = 111
            save_job(job_path, first)
            with mock.patch("auto_resume.registering.process_is_running", return_value=True), \
                 mock.patch("auto_resume.registering.launch_watchdog") as launch:
                register_job(thread_id, project, "new goal", home, start_watchdog=True)
                launch.assert_not_called()
            with mock.patch("auto_resume.registering.process_is_running", return_value=False), \
                 mock.patch("auto_resume.registering.launch_watchdog", return_value=222) as launch:
                register_job(thread_id, project, "newer goal", home, start_watchdog=True)
                launch.assert_called_once_with(job_path)

    def test_concurrent_registration_creates_one_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            thread_id = str(uuid.uuid4())
            results, errors = [], []
            def worker(goal):
                try:
                    results.append(register_job(thread_id, project, goal, home, start_watchdog=False))
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=worker, args=(f"goal {i}",)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            self.assertEqual(1, len({item["job_id"] for item in results}))
            self.assertEqual(1, len(list((home / "auto-resume" / "jobs").glob("*.json"))))

    def test_preflight_opt_out_and_missing_conditions_are_skipped(self):
        self.assertEqual("SKIPPED", preflight(opt_out=True)["outcome"])
        self.assertEqual("SKIPPED", preflight()["outcome"])
        self.assertEqual("SKIPPED", preflight(thread_id="bad", project="missing", goal="x")["outcome"])

    def test_preflight_registers_eligible_task_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            thread_id = str(uuid.uuid4())
            first = preflight(thread_id, project, "goal one", home, start_watchdog=False)
            second = preflight(thread_id, project, "goal two", home, start_watchdog=False)
            self.assertEqual("REGISTERED", first["outcome"])
            self.assertEqual("REUSED", second["outcome"])
            self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])

    @staticmethod
    def job(max_cycles, schema_version):
        now = "2026-01-01T00:00:00+00:00"
        return {
            "schema_version": schema_version, "job_id": "job", "thread_id": str(uuid.uuid4()),
            "project_root": "C:/project", "original_goal": "goal", "status": "RUNNING",
            "billing_policy": "included_only", "limit_id": "codex", "max_cycles": max_cycles,
            "completed_cycles": 0, "poll_interval_seconds": 60, "safety_margin_seconds": 30,
            "checkpoint_path": "C:/checkpoint.md", "expected_repo_snapshot": {},
            "watchdog_pid": None, "created_at": now, "updated_at": now, "last_error": None,
        }


if __name__ == "__main__":
    unittest.main()
