import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.registering import register_job, start_watchdog
from auto_resume.state import (ACTIVE_STATES, REQUIRED_JOB_FIELDS, job_state_locks,
                               load_job, load_json, update_job)
from auto_resume.watch import _set


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

    def test_register_rejects_uppercase_uuid_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            with self.assertRaises(ValueError):
                register_job(str(uuid.uuid4()).upper(), project, "目标", Path(tmp) / "home", start_watchdog=False)

    def test_new_turn_supersedes_only_the_previous_job_in_same_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            home = Path(tmp) / "home"
            project.mkdir()
            self.make_repo(project)
            thread_id = str(uuid.uuid4())
            child_id = str(uuid.uuid4())
            first = register_job(thread_id, project, "first", home, task_id="turn-1",
                                 start_watchdog=False)
            second = register_job(thread_id, project, "second", home, task_id="turn-2",
                                  start_watchdog=False)
            child = register_job(child_id, project, "child", home, task_id="child-turn",
                                 parent_thread_id=thread_id, parent_task_id="turn-2",
                                 root_thread_id=thread_id, start_watchdog=False)
            self.assertNotEqual(first["job_id"], second["job_id"])
            old = load_json(home / "auto-resume" / "jobs" / f"{first['job_id']}.json")
            self.assertEqual("SUPERSEDED", old["status"])
            self.assertEqual(second["job_id"], old["superseded_by"])
            self.assertEqual("REGISTERED", second["status"])
            self.assertEqual("REGISTERED", child["status"])
            self.assertEqual(thread_id, child["root_thread_id"])

    def test_terminal_job_status_is_absorbing_for_watcher_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, home = Path(tmp) / "project", Path(tmp) / "home"
            project.mkdir(); self.make_repo(project)
            job = register_job(str(uuid.uuid4()), project, "goal", home, task_id="turn",
                               start_watchdog=False)
            path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            update_job(path, lambda value: value.update(status="DONE"))
            _set(path, job, "RUNNING")
            self.assertEqual("DONE", load_job(path)["status"])

    def test_job_state_locks_are_canonical_under_reverse_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, home = Path(tmp) / "project", Path(tmp) / "home"
            project.mkdir(); self.make_repo(project)
            paths = []
            for index in range(2):
                job = register_job(str(uuid.uuid4()), project, f"goal-{index}", home,
                                   task_id=f"turn-{index}", start_watchdog=False)
                paths.append(home / "auto-resume" / "jobs" / f"{job['job_id']}.json")
            barrier, finished = threading.Barrier(2), []

            def worker(requested):
                barrier.wait()
                with job_state_locks(requested, timeout=3):
                    time.sleep(0.05)
                finished.append(True)

            threads = [threading.Thread(target=worker, args=(paths,)),
                       threading.Thread(target=worker, args=(list(reversed(paths)),))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(2, len(finished))

    def test_supersede_racing_watcher_running_remains_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, home = Path(tmp) / "project", Path(tmp) / "home"
            project.mkdir(); self.make_repo(project)
            thread = str(uuid.uuid4())
            old = register_job(thread, project, "old", home, task_id="old", start_watchdog=False)
            old_path = home / "auto-resume" / "jobs" / f"{old['job_id']}.json"
            barrier = threading.Barrier(2)
            failures = []

            def supersede():
                try:
                    barrier.wait()
                    register_job(thread, project, "new", home, task_id="new", start_watchdog=False)
                except BaseException as exc:
                    failures.append(exc)

            def watcher():
                try:
                    barrier.wait()
                    _set(old_path, old, "RUNNING")
                except BaseException as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=supersede), threading.Thread(target=watcher)]
            for thread_obj in threads: thread_obj.start()
            for thread_obj in threads: thread_obj.join(timeout=5)
            self.assertFalse(failures)
            self.assertTrue(all(not thread_obj.is_alive() for thread_obj in threads))
            self.assertEqual("SUPERSEDED", load_job(old_path)["status"])

    def test_v2_job_migrates_to_v3_without_changing_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            thread_id = str(uuid.uuid4())
            job = register_job(thread_id, project, "legacy", Path(tmp) / "home",
                               start_watchdog=False)
            path = Path(tmp) / "home" / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            raw = load_json(path)
            old_fields = {
                "schema_version", "job_id", "thread_id", "project_root", "original_goal",
                "status", "billing_policy", "limit_id", "max_cycles", "completed_cycles",
                "poll_interval_seconds", "safety_margin_seconds", "checkpoint_path",
                "expected_repo_snapshot", "watchdog_pid", "created_at", "updated_at", "last_error",
            }
            raw = {key: value for key, value in raw.items() if key in old_fields}
            raw["schema_version"] = 2
            path.write_text(json.dumps(raw), encoding="utf-8")
            from auto_resume.state import load_job
            migrated = load_job(path)
            self.assertEqual(3, migrated["schema_version"])
            self.assertEqual(raw["job_id"], migrated["job_id"])
            self.assertEqual(thread_id, migrated["task_id"])
            self.assertEqual(thread_id, migrated["root_thread_id"])

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
            job["watchdog_pid"] = 1234
            save_job(job_path, job)
            fake_process = mock.Mock(pid=1234)
            fake_process.poll.return_value = None
            fake_uuid = mock.Mock(hex="nonce")
            lease = {"nonce": "nonce", "pid": 1234}
            with mock.patch("auto_resume.registering.subprocess.Popen", return_value=fake_process) as popen, \
                 mock.patch("auto_resume.registering.uuid.uuid4", return_value=fake_uuid), \
                 mock.patch("auto_resume.registering.read_lease", return_value=lease), \
                 mock.patch("auto_resume.registering.watchdog_lease_is_live", return_value=True), \
                 mock.patch("auto_resume.registering._detach_popen"):
                self.assertEqual(1234, start_watchdog(job_path))
            argv = popen.call_args.args[0]
            self.assertEqual("run", argv[2])
            self.assertEqual("--job", argv[3])
            self.assertEqual(1234, load_json(job_path)["watchdog_pid"])


if __name__ == "__main__":
    unittest.main()
