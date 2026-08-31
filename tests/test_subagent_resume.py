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

from auto_resume.handoffs import (consume_handoffs, finalize_handoff, pending_handoffs,
                                  stage_handoff, write_handoff)
from auto_resume.checkpoints import read_checkpoint
from auto_resume.registering import register_job
from auto_resume.resume import _final_text, resume_thread
from auto_resume.state import load_job, load_json, save_job
from auto_resume.watch import active_descendants, run_job


class SubagentResumeTests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def test_handoff_is_atomic_idempotent_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            data = {"parent_thread_id": parent, "parent_task_id": "p", "child_thread_id": child,
                    "child_task_id": "c", "status": "DONE", "final_text": "result"}
            first = write_handoff(home, data)
            second = write_handoff(home, data)
            self.assertEqual(first, second)
            pending = pending_handoffs(home, parent, "p")
            self.assertEqual(1, len(pending))
            consume_handoffs(home, parent, "p", [(pending[0]["path"], pending[0]["revision"])])
            self.assertEqual([], pending_handoffs(home, parent, "p"))

    def test_unconsumed_handoff_merges_late_result_events_and_artifacts_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            base = {"parent_thread_id": parent, "parent_task_id": "p", "child_thread_id": child,
                    "child_task_id": "c", "status": "DONE", "final_text": None,
                    "event_summary": [], "artifacts": []}
            path = stage_handoff(home, base)
            self.assertEqual([], pending_handoffs(home, parent, "p"))
            enriched = {**base, "final_text": "result",
                        "event_summary": ["item.completed", "turn.completed"],
                        "artifacts": ["artifact.txt"]}
            first = finalize_handoff(home, enriched)
            second = finalize_handoff(home, {**enriched, "final_text": "late replacement"})
            value = load_json(path)
            self.assertEqual("result", value["final_text"])
            self.assertEqual(["item.completed", "turn.completed"], value["event_summary"])
            self.assertEqual(["artifact.txt"], value["artifacts"])
            self.assertTrue(value["finalized"])
            self.assertEqual(1, value["revision"])
            self.assertEqual(first["revision"], second["revision"])

    def test_handoff_consumption_requires_matching_revision_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            value = {"parent_thread_id": parent, "parent_task_id": "p", "child_thread_id": child,
                     "child_task_id": "c", "status": "DONE", "final_text": "result"}
            finalized = finalize_handoff(home, value)
            path, revision = finalized["path"], finalized["revision"]
            self.assertEqual([], consume_handoffs(home, parent, "p", [(path, revision + 1)]))
            self.assertEqual(1, len(pending_handoffs(home, parent, "p")))
            self.assertEqual([path], consume_handoffs(home, parent, "p", [(path, revision)]))
            consumed_at = load_json(path)["consumed_at"]
            self.assertEqual([], consume_handoffs(home, parent, "p", [(path, revision)]))
            self.assertEqual(consumed_at, load_json(path)["consumed_at"])

    def test_nested_agent_event_yields_handoff_text(self):
        events = [{"type": "item.completed", "item": {
            "type": "agent_message", "text": "child result"}}]
        self.assertEqual("child result", _final_text(events))

    def test_parent_waits_for_descendant_and_resume_injects_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "p", home, task_id="p", start_watchdog=False)
            register_job(child_thread, repo, "c", home, task_id="c", parent_thread_id=parent_thread,
                         parent_task_id="p", root_thread_id=parent_thread, start_watchdog=False)
            self.assertTrue(active_descendants(parent, home / "auto-resume" / "jobs"))

            fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
            env_log = Path(tmp) / "env.json"
            with unittest.mock.patch.dict(os.environ, {"FAKE_ENV_LOG": str(env_log)}, clear=False):
                result = resume_thread((sys.executable, str(fake)), parent_thread,
                                       "[CODEX_AUTO_RESUME] continue", repo,
                                       env={"CODEX_AUTO_RESUME_JOB_ID": parent["job_id"],
                                            "CODEX_AUTO_RESUME_TASK_ID": "p"})
            self.assertTrue(result.completed)
            self.assertEqual({"CODEX_AUTO_RESUME_JOB_ID": parent["job_id"],
                              "CODEX_AUTO_RESUME_TASK_ID": "p"},
                             json.loads(env_log.read_text(encoding="utf-8")))

    def test_parent_sees_active_grandchild_through_terminal_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            root, child_thread, grandchild_thread = (str(uuid.uuid4()) for _ in range(3))
            parent = register_job(root, repo, "p", home, task_id="p", start_watchdog=False)
            child = register_job(child_thread, repo, "c", home, task_id="c",
                                 parent_thread_id=root, parent_task_id="p", root_thread_id=root,
                                 start_watchdog=False)
            child["status"] = "DONE"
            save_job(home / "auto-resume" / "jobs" / f"{child['job_id']}.json", child)
            register_job(grandchild_thread, repo, "g", home, task_id="g",
                         parent_thread_id=child_thread, parent_task_id="c", root_thread_id=root,
                         start_watchdog=False)
            descendants = active_descendants(parent, home / "auto-resume" / "jobs")
            self.assertEqual(["g"], [item["task_id"] for item in descendants])

    def test_resumed_child_process_can_checkpoint_while_project_is_leased(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            job = register_job(thread, repo, "continue", home, task_id="turn-a", start_watchdog=False)
            job["status"] = "WAITING_RESET"
            job_path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            save_job(job_path, job)
            fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
            checkpoint = SCRIPTS / "checkpoint.py"
            command = [sys.executable, str(checkpoint), "--job-id", job["job_id"],
                       "--codex-home", str(home), "--set", "CURRENT_STATE=checkpoint-from-child"]
            env = {"FAKE_LIMITS": json.dumps({"rateLimits": {"primary": {
                       "usedPercent": 0, "resetsAt": None}}}),
                   "FAKE_THREAD_ID": thread,
                   "FAKE_CHECKPOINT_COMMAND_JSON": json.dumps(command)}
            with unittest.mock.patch.dict(os.environ, env, clear=False), \
                    unittest.mock.patch("auto_resume.watch._settled",
                                        return_value=job["expected_repo_snapshot"]):
                self.assertEqual("RUNNING", run_job(job_path, (sys.executable, str(fake)), once=True))
            self.assertEqual("checkpoint-from-child",
                             read_checkpoint(job["checkpoint_path"])["CURRENT_STATE"])

    def test_child_done_checkpoint_late_message_is_fully_consumed_by_parent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="p",
                                  start_watchdog=False)
            child = register_job(child_thread, repo, "child", home, task_id="c",
                                 parent_thread_id=parent_thread, parent_task_id="p",
                                 root_thread_id=parent_thread, start_watchdog=False,
                                 association_source="explicit", fork_timestamp=100)
            jobs = home / "auto-resume" / "jobs"
            parent_path, child_path = (jobs / f"{item['job_id']}.json" for item in (parent, child))
            for path, job in ((parent_path, parent), (child_path, child)):
                job["status"] = "WAITING_RESET"; save_job(path, job)
            fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
            checkpoint = SCRIPTS / "checkpoint.py"
            child_checkpoint = [sys.executable, str(checkpoint), "--job-id", child["job_id"],
                                "--codex-home", str(home), "--set", "AUTO_RESUME_STATUS=DONE"]
            common_limits = json.dumps({"rateLimits": {"primary": {
                "usedPercent": 0, "resetsAt": None}}})
            after_checkpoint, release_result = Path(tmp) / "checkpoint.signal", Path(tmp) / "result.release"
            child_env = {"FAKE_LIMITS": common_limits, "FAKE_THREAD_ID": child_thread,
                         "FAKE_CHECKPOINT_COMMAND_JSON": json.dumps(child_checkpoint),
                         "FAKE_AGENT_MESSAGE": "complete child result",
                         "FAKE_ARTIFACT_PATH": "artifact.txt",
                         "FAKE_AFTER_CHECKPOINT_SIGNAL": str(after_checkpoint),
                         "FAKE_RESULT_RELEASE": str(release_result)}
            with unittest.mock.patch.dict(os.environ, child_env, clear=False), \
                    unittest.mock.patch("auto_resume.watch._settled",
                                        return_value=child["expected_repo_snapshot"]):
                outcome = []
                worker = threading.Thread(target=lambda: outcome.append(
                    run_job(child_path, (sys.executable, str(fake)), once=True)))
                worker.start()
                deadline = time.monotonic() + 10
                while not after_checkpoint.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(after_checkpoint.exists())
                self.assertTrue(active_descendants(parent, jobs))
                self.assertEqual([], pending_handoffs(home, parent_thread, "p"))
                release_result.write_text("go", encoding="utf-8")
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(["DONE"], outcome)
            pending = pending_handoffs(home, parent_thread, "p")
            self.assertEqual(1, len(pending))
            self.assertEqual("complete child result", pending[0]["final_text"])
            self.assertIn("item.completed", pending[0]["event_summary"])
            self.assertEqual(["artifact.txt"], pending[0]["artifacts"])

            argv_log = Path(tmp) / "parent-argv.json"
            parent_env = {"FAKE_LIMITS": common_limits, "FAKE_THREAD_ID": parent_thread,
                          "FAKE_CHECKPOINT_COMMAND_JSON": "", "FAKE_AGENT_MESSAGE": "parent merged",
                          "FAKE_ARTIFACT_PATH": "", "FAKE_ARGV_LOG": str(argv_log)}
            with unittest.mock.patch.dict(os.environ, parent_env, clear=False), \
                    unittest.mock.patch("auto_resume.watch._settled",
                                        return_value=parent["expected_repo_snapshot"]):
                self.assertEqual("RUNNING", run_job(parent_path, (sys.executable, str(fake)), once=True))
            self.assertEqual([], pending_handoffs(home, parent_thread, "p"))
            handoff_path = Path(pending[0]["path"])
            consumed = load_json(handoff_path)
            consumed_at = consumed["consumed_at"]
            self.assertTrue(consumed["consumed"])
            prompt = json.loads(argv_log.read_text(encoding="utf-8"))[3]
            prompt_lines = prompt.splitlines()
            path_line = next(line for line in prompt_lines if line == str(handoff_path.resolve()))
            revision_line = prompt_lines[prompt_lines.index(path_line) + 1]
            self.assertEqual(f"revision={pending[0]['revision']}", revision_line)
            self.assertEqual("complete child result",
                             json.loads(Path(path_line).read_text(encoding="utf-8"))["final_text"])
            self.assertEqual([], consume_handoffs(home, parent_thread, "p"))
            self.assertEqual(consumed_at, load_json(handoff_path)["consumed_at"])
            self.assertEqual(1, load_job(parent_path)["completed_cycles"])

    def test_child_registration_after_parent_claim_preempts_before_spawn(self):
        from auto_resume import watch
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="p",
                                  poll_interval_seconds=1, start_watchdog=False)
            parent_path = home / "auto-resume" / "jobs" / f"{parent['job_id']}.json"
            parent["status"] = "WAITING_RESET"; save_job(parent_path, parent)
            entered, release, outcome = threading.Event(), threading.Event(), []
            original_record = watch.record_resume_launch

            def paused_record(codex_home, job):
                launch_id = original_record(codex_home, job)
                entered.set(); release.wait(5)
                return launch_id

            fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
            env = {"FAKE_LIMITS": json.dumps({"rateLimits": {"primary": {
                       "usedPercent": 0, "resetsAt": None}}}),
                   "FAKE_THREAD_ID": parent_thread,
                   "FAKE_ARGV_LOG": str(Path(tmp) / "argv.json")}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("auto_resume.watch._settled",
                               return_value=parent["expected_repo_snapshot"]), \
                    mock.patch("auto_resume.watch.record_resume_launch", side_effect=paused_record):
                worker = threading.Thread(target=lambda: outcome.append(
                    run_job(parent_path, (sys.executable, str(fake)), once=True)))
                worker.start(); self.assertTrue(entered.wait(5))
                child = register_job(child_thread, repo, "child", home, task_id="c",
                                     parent_thread_id=parent_thread, parent_task_id="p",
                                     root_thread_id=parent_thread, start_watchdog=False)
                lease = load_json(watch._project_lease(parent, home))
                self.assertIn(child["job_id"], lease["descendant_pending"])
                release.set(); worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(["WAITING_RESET"], outcome)
            self.assertFalse(Path(env["FAKE_ARGV_LOG"]).exists())

    def test_child_registration_during_resume_preempts_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="p",
                                  poll_interval_seconds=1, start_watchdog=False)
            parent_path = home / "auto-resume" / "jobs" / f"{parent['job_id']}.json"
            parent["status"] = "WAITING_RESET"; save_job(parent_path, parent)
            argv_log, outcome = Path(tmp) / "argv.json", []
            fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
            env = {"FAKE_LIMITS": json.dumps({"rateLimits": {"primary": {
                       "usedPercent": 0, "resetsAt": None}}}),
                   "FAKE_THREAD_ID": parent_thread, "FAKE_ARGV_LOG": str(argv_log),
                   "FAKE_RESUME_SLEEP": "5"}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("auto_resume.watch._settled",
                               return_value=parent["expected_repo_snapshot"]):
                worker = threading.Thread(target=lambda: outcome.append(
                    run_job(parent_path, (sys.executable, str(fake)), once=True)))
                worker.start()
                deadline = time.monotonic() + 5
                while not argv_log.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(argv_log.exists())
                register_job(child_thread, repo, "child", home, task_id="c",
                             parent_thread_id=parent_thread, parent_task_id="p",
                             root_thread_id=parent_thread, start_watchdog=False)
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(["WAITING_RESET"], outcome)

    def test_child_registration_before_parent_commit_discards_parent_result(self):
        from auto_resume.resume import ResumeResult
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="p",
                                  poll_interval_seconds=1, start_watchdog=False)
            parent_path = home / "auto-resume" / "jobs" / f"{parent['job_id']}.json"
            parent["status"] = "WAITING_RESET"; save_job(parent_path, parent)

            def resume_then_register(*_args, **_kwargs):
                register_job(child_thread, repo, "child", home, task_id="c",
                             parent_thread_id=parent_thread, parent_task_id="p",
                             root_thread_id=parent_thread, start_watchdog=False)
                return ResumeResult(completed=True, returncode=0,
                                    events=[{"type": "turn.completed"}], final_text="parent result")

            with mock.patch("auto_resume.watch.read_limits") as limits, \
                    mock.patch("auto_resume.watch.reset_deadline", return_value=None), \
                    mock.patch("auto_resume.watch._settled",
                               return_value=parent["expected_repo_snapshot"]), \
                    mock.patch("auto_resume.watch.resume_thread", side_effect=resume_then_register):
                limits.return_value = mock.Mock(limit_id="codex", primary=None, secondary=None)
                self.assertEqual("WAITING_RESET", run_job(parent_path, ("codex",), once=True))
            saved = load_job(parent_path)
            self.assertEqual("WAITING_RESET", saved["status"])
            self.assertEqual(0, saved["completed_cycles"])


if __name__ == "__main__":
    unittest.main()
