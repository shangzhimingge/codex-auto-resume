import json
import datetime
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

from auto_resume.activation import preflight
from auto_resume.daemon import scan_once
from auto_resume.registering import register_job
from auto_resume.state import load_job, load_json, save_job, update_job
from auto_resume.session_tasks import (discover_session_updates, record_resume_launch,
                                       confirm_resume_launch_for_job, resolve_current_task)


def line(kind, payload):
    timestamp = "2026-08-30T00:00:00Z"
    if (kind == "event_msg" and isinstance(payload, dict) and
            payload.get("type") == "task_started" and
            isinstance(payload.get("started_at"), (int, float)) and
            not isinstance(payload.get("started_at"), bool)):
        timestamp = datetime.datetime.fromtimestamp(
            float(payload["started_at"]), datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload})


class SessionTaskTests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def write_rollout(self, sessions, thread, rows):
        path = sessions / "2026" / "08" / "30" / f"rollout-x-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_resolve_uses_first_child_meta_and_latest_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            child, parent = str(uuid.uuid4()), str(uuid.uuid4())
            path = self.write_rollout(sessions, child, [
                line("session_meta", {"id": child, "cwd": str(repo), "parent_thread_id": parent,
                                       "agent_path": "root/worker"}),
                line("session_meta", {"id": parent, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "child-turn"}),
                line("turn_context", {"turn_id": "child-turn", "cwd": str(repo)}),
                line("event_msg", {"type": "agent_message", "message": "do child work"}),
            ])
            task = resolve_current_task(thread_id=child, sessions_root=sessions)
            self.assertEqual(child, task["thread_id"])
            self.assertEqual("child-turn", task["task_id"])
            self.assertEqual(parent, task["parent_thread_id"])
            self.assertEqual(path.resolve(), Path(task["rollout_path"]))

    def test_preflight_discovers_thread_task_project_and_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            home = Path(tmp) / "home"
            thread = str(uuid.uuid4())
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
                line("turn_context", {"turn_id": "turn-a", "cwd": str(repo)}),
                line("event_msg", {"type": "user_message", "message": "ship it"}),
            ])
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}, clear=False):
                result = preflight(codex_home=home, sessions_root=sessions, start_watchdog=False)
            self.assertEqual("REGISTERED", result["outcome"])
            self.assertEqual("turn-a", result["job"]["task_id"])
            self.assertEqual("ship it", result["job"]["original_goal"])

    def test_internal_resume_preflight_reuses_original_job_and_records_new_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            original = register_job(thread, repo, "original", home, task_id="turn-original",
                                    start_watchdog=False)
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-resume"}),
                line("turn_context", {"turn_id": "turn-resume", "cwd": str(repo)}),
                line("event_msg", {"type": "user_message", "message": "[CODEX_AUTO_RESUME] continue"}),
            ])
            old = Path.cwd(); os.chdir(repo)
            try:
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread,
                        "CODEX_AUTO_RESUME_JOB_ID": original["job_id"],
                        "CODEX_AUTO_RESUME_TASK_ID": "turn-original"}, clear=False):
                    result = preflight(codex_home=home, sessions_root=sessions, start_watchdog=False)
            finally:
                os.chdir(old)
            self.assertEqual("REUSED", result["outcome"])
            jobs = list((home / "auto-resume" / "jobs").glob("*.json"))
            self.assertEqual(1, len(jobs))
            self.assertEqual("turn-original", load_job(jobs[0])["task_id"])
            attempts = load_json(home / "auto-resume" / "state" / "resume-attempts.json")
            self.assertEqual("turn-resume", attempts[original["job_id"]][-1]["resume_turn_id"])

    def test_concurrent_scanner_waits_for_input_and_launch_record_suppresses_resume_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            original = register_job(thread, repo, "original", home, task_id="original",
                                    start_watchdog=False)
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "resume-race",
                                   "started_at": time.time() + 0.01}),
            ])
            record_resume_launch(home, original)
            observed = []
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                worker = threading.Thread(target=lambda: observed.append(scan_once(home, sessions_root=sessions)))
                worker.start(); worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(0, observed[0]["registered"])
                self.assertEqual(1, len(list((home / "auto-resume" / "jobs").glob("*.json"))))
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line("event_msg", {"type": "user_message",
                                                      "message": "[CODEX_AUTO_RESUME] continue"}) + "\n")
                again = scan_once(home, sessions_root=sessions)
            self.assertEqual(0, again["registered"])
            self.assertEqual(1, len(list((home / "auto-resume" / "jobs").glob("*.json"))))

    def test_user_turn_started_before_launch_is_released_and_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread, started = str(uuid.uuid4()), time.time() - 0.2
            old = register_job(thread, repo, "old", home, task_id="old", start_watchdog=False)
            old_path = home / "auto-resume" / "jobs" / f"{old['job_id']}.json"
            old["status"] = "WAITING_RESET"; save_job(old_path, old)
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "user-turn",
                                   "started_at": started}),
            ])
            record_resume_launch(home, old)
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                first = scan_once(home, sessions_root=sessions)
                self.assertEqual(0, first["registered"])
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line("event_msg", {"type": "user_message",
                                                      "message": "new user goal"}) + "\n")
                second = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, second["registered"])
            self.assertEqual("SUPERSEDED", load_job(old_path)["status"])
            launches = load_json(home / "auto-resume" / "state" / "resume-launches.json")["launches"]
            self.assertEqual("pending", launches[-1]["claim_state"])

    def test_resume_prefix_is_provisional_until_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            old = register_job(thread, repo, "old", home, task_id="old", start_watchdog=False)
            launch_id = record_resume_launch(home, old)
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "resume-turn",
                                   "started_at": time.time() + 0.01}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                first = scan_once(home, sessions_root=sessions)
                self.assertEqual(0, first["registered"])
                cursors = load_json(home / "auto-resume" / "state" / "session-cursors.json")
                self.assertFalse(any("resume-turn" in item for item in cursors["seen"]))
                provisional = load_json(home / "auto-resume" / "state" / "resume-launches.json")
                launch = next(item for item in provisional["launches"] if item["launch_id"] == launch_id)
                self.assertEqual("provisional", launch["claim_state"])
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line("event_msg", {"type": "user_message",
                                                      "message": "[CODEX_AUTO_RESUME] continue"}) + "\n")
                second = scan_once(home, sessions_root=sessions)
            self.assertEqual(0, second["registered"])
            confirmed = load_json(home / "auto-resume" / "state" / "resume-launches.json")
            launch = next(item for item in confirmed["launches"] if item["launch_id"] == launch_id)
            self.assertEqual("confirmed", launch["claim_state"])
            self.assertEqual("resume-turn", launch["confirmed_turn_id"])

    def test_marker_in_first_batch_requires_provisional_then_confirmed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            old = register_job(thread, repo, "old", home, task_id="old", start_watchdog=False)
            launch_id = record_resume_launch(home, old)
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "resume-turn",
                                   "started_at": time.time() + 0.01}),
                line("event_msg", {"type": "user_message",
                                   "message": "[CODEX_AUTO_RESUME] continue"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                result = scan_once(home, sessions_root=sessions)
            self.assertEqual(0, result["registered"])
            self.assertEqual([old["job_id"]], [load_job(path)["job_id"] for path in
                                               (home / "auto-resume" / "jobs").glob("*.json")])
            launch = next(item for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                          if item["launch_id"] == launch_id)
            self.assertEqual("confirmed", launch["claim_state"])
            self.assertEqual("resume-turn", launch["provisional_turn_id"])
            self.assertEqual("resume-turn", launch["confirmed_turn_id"])

    def test_unmatched_user_message_with_marker_text_is_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "user-turn"}),
                line("event_msg", {"type": "user_message",
                                   "message": "Explain the literal [CODEX_AUTO_RESUME] marker"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                result = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, result["registered"])
            job = load_job(next((home / "auto-resume" / "jobs").glob("*.json")))
            self.assertEqual("user-turn", job["task_id"])
            self.assertIn("literal [CODEX_AUTO_RESUME]", job["original_goal"])

    def test_internal_preflight_confirms_exact_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            old = register_job(thread, repo, "old", home, task_id="old", start_watchdog=False)
            launch_id = record_resume_launch(home, old)
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "resume-turn",
                                   "started_at": time.time() + 0.01}),
            ])
            discover_session_updates(home, sessions)
            old_cwd = Path.cwd(); os.chdir(repo)
            try:
                env = {"CODEX_THREAD_ID": thread, "CODEX_AUTO_RESUME_JOB_ID": old["job_id"],
                       "CODEX_AUTO_RESUME_TASK_ID": old["task_id"]}
                with mock.patch.dict(os.environ, env, clear=False):
                    result = preflight(codex_home=home, sessions_root=sessions,
                                       start_watchdog=False)
            finally:
                os.chdir(old_cwd)
            self.assertEqual("REUSED", result["outcome"])
            launches = load_json(home / "auto-resume" / "state" / "resume-launches.json")["launches"]
            launch = next(item for item in launches if item["launch_id"] == launch_id)
            self.assertEqual("confirmed", launch["claim_state"])
            self.assertEqual("resume-turn", launch["confirmed_turn_id"])

    def test_internal_preflight_before_daemon_scan_claims_opaque_child_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="parent",
                                  start_watchdog=False)
            child = register_job(child_thread, repo, "child", home, task_id="child-old",
                                 parent_thread_id=parent_thread, parent_task_id=parent["task_id"],
                                 root_thread_id=parent_thread, start_watchdog=False)
            child_path = home / "auto-resume" / "jobs" / f"{child['job_id']}.json"
            launch_id = record_resume_launch(home, child)
            launched_at = next(item["launched_at"] for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                               if item["launch_id"] == launch_id)
            rollout = self.write_rollout(sessions, child_thread, [
                line("session_meta", {"id": child_thread, "cwd": str(repo),
                                      "parent_thread_id": parent_thread,
                                      "parent_task_id": parent["task_id"]}),
                line("event_msg", {"type": "task_started", "turn_id": "opaque-turn",
                                   "started_at": launched_at + 0.01}),
            ])
            old_cwd = Path.cwd(); os.chdir(repo)
            try:
                env = {"CODEX_THREAD_ID": child_thread,
                       "CODEX_AUTO_RESUME_JOB_ID": child["job_id"],
                       "CODEX_AUTO_RESUME_TASK_ID": child["task_id"]}
                with mock.patch.dict(os.environ, env, clear=False):
                    result = preflight(codex_home=home, sessions_root=sessions,
                                       start_watchdog=False)
            finally:
                os.chdir(old_cwd)
            self.assertEqual("REUSED", result["outcome"])
            claim = next(item for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                         if item["launch_id"] == launch_id)
            self.assertEqual("opaque-turn", claim["provisional_turn_id"])
            self.assertEqual("opaque-turn", claim["confirmed_turn_id"])
            self.assertEqual("confirmed", claim["claim_state"])

            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(line("event_msg", {"type": "agent_message",
                                                  "encrypted_content": "opaque"}) + "\n")
                handle.write(line("event_msg", {"type": "token_count",
                                                  "rate_limit_reached": True}) + "\n")
                handle.write(line("event_msg", {"type": "task_complete",
                                                  "last_agent_message": None}) + "\n")
            before_status = load_job(child_path)["status"]
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                scanned = scan_once(home, sessions_root=sessions)
            self.assertEqual(0, scanned["registered"])
            child_jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")
                          if load_job(path)["thread_id"] == child_thread]
            self.assertEqual(["child-old"], [job["task_id"] for job in child_jobs])
            self.assertEqual(before_status, load_job(child_path)["status"])
            cursors = load_json(home / "auto-resume" / "state" / "session-cursors.json")
            current = next(value["fsm"]["current"] for value in cursors["files"].values()
                           if value["fsm"]["meta"]["id"] == child_thread)
            self.assertEqual("confirmed", current["launch_claim_state"])
            self.assertTrue(current["internal_resume"])
            self.assertTrue(any("opaque-turn:True:True" in item for item in cursors["seen"]))

    def test_task_started_prefers_top_level_milliseconds_over_payload_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="parent",
                                  start_watchdog=False)
            child = register_job(child_thread, repo, "child", home, task_id="child-old",
                                 parent_thread_id=parent_thread, parent_task_id=parent["task_id"],
                                 root_thread_id=parent_thread, start_watchdog=False)
            child_path = home / "auto-resume" / "jobs" / f"{child['job_id']}.json"
            launch_id = record_resume_launch(home, child)
            launched_at = next(item["launched_at"] for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                               if item["launch_id"] == launch_id)
            top_started = launched_at + 0.005
            top_timestamp = datetime.datetime.fromtimestamp(
                top_started, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            started_row = json.dumps({
                "timestamp": top_timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "same-second-turn",
                            "started_at": int(launched_at)},
            })
            rollout = self.write_rollout(sessions, child_thread, [
                line("session_meta", {"id": child_thread, "cwd": str(repo),
                                      "parent_thread_id": parent_thread,
                                      "parent_task_id": parent["task_id"]}),
                started_row,
            ])
            old_cwd = Path.cwd(); os.chdir(repo)
            try:
                env = {"CODEX_THREAD_ID": child_thread,
                       "CODEX_AUTO_RESUME_JOB_ID": child["job_id"],
                       "CODEX_AUTO_RESUME_TASK_ID": child["task_id"]}
                with mock.patch.dict(os.environ, env, clear=False):
                    result = preflight(codex_home=home, sessions_root=sessions,
                                       start_watchdog=False)
            finally:
                os.chdir(old_cwd)
            self.assertEqual("REUSED", result["outcome"])
            claim = next(item for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                         if item["launch_id"] == launch_id)
            self.assertEqual("confirmed", claim["claim_state"])
            self.assertEqual("same-second-turn", claim["confirmed_turn_id"])

            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(line("event_msg", {"type": "agent_message",
                                                  "encrypted_content": "opaque"}) + "\n")
                handle.write(line("event_msg", {"type": "token_count",
                                                  "rate_limit_reached": True}) + "\n")
                handle.write(line("event_msg", {"type": "task_complete",
                                                  "last_agent_message": None}) + "\n")
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                scanned = scan_once(home, sessions_root=sessions)
            self.assertEqual(0, scanned["registered"])
            child_jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")
                          if load_job(path)["thread_id"] == child_thread]
            self.assertEqual(["child-old"], [job["task_id"] for job in child_jobs])
            self.assertNotEqual("SUPERSEDED", load_job(child_path)["status"])

    def test_internal_preflight_zero_or_ambiguous_launch_does_not_mutate_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            thread = str(uuid.uuid4())
            job = {"job_id": "job", "task_id": "task", "thread_id": thread}
            record_resume_launch(home, job)
            launches_path = home / "auto-resume" / "state" / "resume-launches.json"
            launched_at = load_json(launches_path)["launches"][0]["launched_at"]
            before = launches_path.read_bytes()
            self.assertIsNone(confirm_resume_launch_for_job(
                home, "different-job", "task", thread, "turn", launched_at))
            self.assertEqual(before, launches_path.read_bytes())

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            thread = str(uuid.uuid4())
            job = {"job_id": "job", "task_id": "task", "thread_id": thread}
            record_resume_launch(home, job); record_resume_launch(home, job)
            launches_path = home / "auto-resume" / "state" / "resume-launches.json"
            launched_at = max(item["launched_at"] for item in load_json(launches_path)["launches"])
            before = launches_path.read_bytes()
            self.assertIsNone(confirm_resume_launch_for_job(
                home, "job", "task", thread, "turn", launched_at + 0.01))
            self.assertEqual(before, launches_path.read_bytes())
            launches = load_json(launches_path)["launches"]
            self.assertEqual(["pending", "pending"], [item["claim_state"] for item in launches])
            self.assertTrue(all(item["provisional_turn_id"] is None for item in launches))

    def test_internal_preflight_pending_launch_clock_boundaries(self):
        cases = (("equal", lambda value: value, True),
                 ("earlier", lambda value: value - 0.001, False),
                 ("missing", lambda _value: None, False),
                 ("bool", lambda _value: True, False),
                 ("nan", lambda _value: float("nan"), False),
                 ("positive_inf", lambda _value: float("inf"), False),
                 ("negative_inf", lambda _value: float("-inf"), False))
        for name, started, accepted in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                thread = str(uuid.uuid4())
                job = {"job_id": "job", "task_id": "task", "thread_id": thread}
                record_resume_launch(home, job)
                launches_path = home / "auto-resume" / "state" / "resume-launches.json"
                launched_at = load_json(launches_path)["launches"][0]["launched_at"]
                result = confirm_resume_launch_for_job(
                    home, "job", "task", thread, "turn", started(launched_at))
                claim = load_json(launches_path)["launches"][0]
                self.assertEqual(accepted, result is not None)
                self.assertEqual("confirmed" if accepted else "pending", claim["claim_state"])

    def test_scanner_and_internal_preflight_converge_on_one_confirmed_claim(self):
        from auto_resume import session_tasks
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, repo, "parent", home, task_id="parent",
                                  start_watchdog=False)
            child = register_job(child_thread, repo, "child", home, task_id="child-old",
                                 parent_thread_id=parent_thread, parent_task_id=parent["task_id"],
                                 root_thread_id=parent_thread, start_watchdog=False)
            child_path = home / "auto-resume" / "jobs" / f"{child['job_id']}.json"
            launch_id = record_resume_launch(home, child)
            launched_at = next(item["launched_at"] for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                               if item["launch_id"] == launch_id)
            self.write_rollout(sessions, child_thread, [
                line("session_meta", {"id": child_thread, "cwd": str(repo),
                                      "parent_thread_id": parent_thread,
                                      "parent_task_id": parent["task_id"]}),
                line("event_msg", {"type": "task_started", "turn_id": "race-turn",
                                   "started_at": launched_at + 0.01}),
            ])
            provisioned, release, failures, results = threading.Event(), threading.Event(), [], []
            original_provision = session_tasks.provision_resume_launch

            def paused_provision(*args, **kwargs):
                claim = original_provision(*args, **kwargs)
                provisioned.set(); release.wait(5)
                return claim

            def scan():
                try:
                    results.append(discover_session_updates(home, sessions))
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=scan)
            with mock.patch("auto_resume.session_tasks.provision_resume_launch",
                            side_effect=paused_provision):
                worker.start(); self.assertTrue(provisioned.wait(5))
                old_cwd = Path.cwd(); os.chdir(repo)
                try:
                    env = {"CODEX_THREAD_ID": child_thread,
                           "CODEX_AUTO_RESUME_JOB_ID": child["job_id"],
                           "CODEX_AUTO_RESUME_TASK_ID": child["task_id"]}
                    with mock.patch.dict(os.environ, env, clear=False):
                        results.append(preflight(codex_home=home, sessions_root=sessions,
                                                 start_watchdog=False))
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    os.chdir(old_cwd); release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], failures)
            claim = next(item for item in load_json(
                home / "auto-resume" / "state" / "resume-launches.json")["launches"]
                         if item["launch_id"] == launch_id)
            self.assertEqual("confirmed", claim["claim_state"])
            self.assertEqual("race-turn", claim["confirmed_turn_id"])
            child_jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")
                          if load_job(path)["thread_id"] == child_thread]
            self.assertEqual(1, len(child_jobs))
            self.assertNotEqual("SUPERSEDED", load_job(child_path)["status"])

    def test_confirmed_launch_refreshes_persisted_fsm_for_encrypted_limit_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            old = register_job(thread, repo, "old", home, task_id="old", start_watchdog=False)
            record_resume_launch(home, old)
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "resume-turn",
                                   "started_at": time.time() + 0.01}),
            ])
            discover_session_updates(home, sessions)
            old_cwd = Path.cwd(); os.chdir(repo)
            try:
                env = {"CODEX_THREAD_ID": thread, "CODEX_AUTO_RESUME_JOB_ID": old["job_id"],
                       "CODEX_AUTO_RESUME_TASK_ID": old["task_id"]}
                with mock.patch.dict(os.environ, env, clear=False):
                    self.assertEqual("REUSED", preflight(codex_home=home, sessions_root=sessions,
                                                         start_watchdog=False)["outcome"])
            finally:
                os.chdir(old_cwd)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line("event_msg", {"type": "agent_message",
                                                  "encrypted_content": "ciphertext"}) + "\n")
                handle.write(line("event_msg", {"type": "token_count",
                                                  "rate_limit_reached": True}) + "\n")
            result = discover_session_updates(home, sessions)
            self.assertEqual([], result["tasks"])
            cursors = load_json(home / "auto-resume" / "state" / "session-cursors.json")
            current = next(iter(cursors["files"].values()))["fsm"]["current"]
            self.assertEqual("confirmed", current["launch_claim_state"])
            self.assertTrue(current["internal_resume"])
            self.assertTrue(any("resume-turn:False:True" in item for item in cursors["seen"]))

    def test_discovery_tolerates_partial_line_and_reports_interrupted_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
                line("event_msg", {"type": "user_message", "message": "goal"}),
                line("event_msg", {"type": "token_count", "rate_limit_reached": True}),
                line("event_msg", {"type": "task_complete", "turn_id": "turn-a",
                                   "last_agent_message": None}),
            ])
            with path.open("ab") as handle:
                handle.write(b'{"type":"event_msg"')
            result = discover_session_updates(codex_home=home, sessions_root=sessions)
            self.assertEqual(1, len(result["tasks"]))
            self.assertTrue(result["tasks"][0]["interrupted"])
            again = discover_session_updates(codex_home=home, sessions_root=sessions)
            self.assertEqual([], again["tasks"])

    def test_secondary_limit_bucket_at_eof_is_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
                line("event_msg", {"type": "user_message", "message": "goal"}),
                line("event_msg", {"type": "token_count", "limits": {
                    "primary": {"used_percent": 12}, "secondary": {"used_percent": 100}}}),
            ])
            result = discover_session_updates(home, sessions)
            self.assertEqual(1, len(result["tasks"]))
            self.assertTrue(result["tasks"][0]["interrupted"])

    def test_incremental_fsm_handles_large_growth_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            filler = line("event_msg", {"type": "response_item", "text": "x" * 2000}) + "\n"
            path = self.write_rollout(sessions, thread, [line("session_meta", {"id": thread, "cwd": str(repo)})])
            with path.open("a", encoding="utf-8") as handle:
                for _ in range(2200): handle.write(filler)
                handle.write(line("event_msg", {"type": "task_started", "turn_id": "large-turn"}) + "\n")
                handle.write(line("event_msg", {"type": "user_message", "message": "large goal"}) + "\n")
            first = discover_session_updates(home, sessions)
            self.assertEqual(["large-turn"], [item["task_id"] for item in first["tasks"]])
            with path.open("a", encoding="utf-8") as handle:
                for _ in range(2200): handle.write(filler)
                handle.write(line("event_msg", {"type": "token_count", "secondary": {"used_percent": 100}}) + "\n")
            midway = discover_session_updates(home, sessions)
            self.assertEqual([], midway["tasks"])
            # A new process loads the persisted FSM/cursor and consumes the remainder.
            final = discover_session_updates(home, sessions)
            self.assertEqual(1, len(final["tasks"]))
            self.assertTrue(final["tasks"][0]["interrupted"])

    def test_daemon_discovers_then_reconciles_completed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            path = self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
                line("turn_context", {"turn_id": "turn-a", "cwd": str(repo)}),
                line("event_msg", {"type": "user_message", "message": "goal"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                first = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, first["registered"])
            job_path = next((home / "auto-resume" / "jobs").glob("*.json"))
            self.assertEqual("turn-a", load_job(job_path)["task_id"])
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line("event_msg", {"type": "task_complete", "turn_id": "turn-a",
                                                "last_agent_message": "done"}) + "\n")
            second = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, second["reconciled"])
            self.assertEqual("DONE", load_job(job_path)["status"])

    def test_daemon_links_child_to_parent_turn_containing_fork_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            register_job(parent, repo, "old", home, task_id="parent-old", start_watchdog=False)
            self.write_rollout(sessions, parent, [
                line("session_meta", {"id": parent, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "parent-old", "started_at": 100}),
                line("event_msg", {"type": "user_message", "message": "old"}),
                line("event_msg", {"type": "task_complete", "completed_at": 150,
                                   "last_agent_message": "done"}),
                line("event_msg", {"type": "task_started", "turn_id": "parent-new", "started_at": 200}),
                line("event_msg", {"type": "user_message", "message": "new"}),
            ])
            self.write_rollout(sessions, child, [
                line("session_meta", {"id": child, "cwd": str(repo), "forked_from_id": parent,
                                      "timestamp": 250, "agent_path": "root/worker"}),
                line("event_msg", {"type": "task_started", "turn_id": "child-turn", "started_at": 251}),
                line("event_msg", {"type": "agent_message", "message": "child work"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                scan_once(home, sessions_root=sessions)
            jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")]
            child_job = next(job for job in jobs if job["task_id"] == "child-turn")
            self.assertEqual("parent-new", child_job["parent_task_id"])
            self.assertEqual("fork_timestamp", child_job["association_source"])

    def test_preflight_child_fork_overrides_heuristic_after_daemon_stop_and_parent_new_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            old = register_job(parent, repo, "old", home, task_id="parent-old", start_watchdog=False)
            parent_path = self.write_rollout(sessions, parent, [
                line("session_meta", {"id": parent, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "parent-old", "started_at": 100}),
                line("event_msg", {"type": "user_message", "message": "old"}),
            ])
            self.write_rollout(sessions, child, [
                line("session_meta", {"id": child, "cwd": str(repo), "forked_from_id": parent,
                                      "timestamp": 150, "agent_path": "root/worker"}),
                line("event_msg", {"type": "task_started", "turn_id": "child-turn", "started_at": 151}),
                line("event_msg", {"type": "agent_message", "message": "child work"}),
            ])
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": child}, clear=False):
                child_result = preflight(codex_home=home, sessions_root=sessions, start_watchdog=False)
            child_job = child_result["job"]
            self.assertEqual(150, child_job["fork_timestamp"])
            with parent_path.open("a", encoding="utf-8") as handle:
                handle.write(line("event_msg", {"type": "task_complete", "completed_at": 175,
                                                  "last_agent_message": "done"}) + "\n")
                handle.write(line("event_msg", {"type": "task_started", "turn_id": "parent-new",
                                                  "started_at": 200}) + "\n")
                handle.write(line("event_msg", {"type": "user_message", "message": "new"}) + "\n")
            new_parent = register_job(parent, repo, "new", home, task_id="parent-new", start_watchdog=False)
            # Model the old active-parent heuristic that may have been persisted
            # by an earlier preflight build while the daemon was stopped.
            register_job(child, repo, "child work", home, task_id="child-turn", start_watchdog=False,
                         parent_thread_id=parent, parent_task_id="parent-new", root_thread_id=parent,
                         fork_timestamp=150, association_source="heuristic_active")
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                scan_once(home, sessions_root=sessions)
            repaired = load_job(home / "auto-resume" / "jobs" / f"{child_job['job_id']}.json")
            self.assertEqual(old["task_id"], repaired["parent_task_id"])
            self.assertEqual("fork_timestamp", repaired["association_source"])
            self.assertEqual("SUPERSEDED", load_job(home / "auto-resume" / "jobs" /
                                                    f"{old['job_id']}.json")["status"])
            self.assertEqual("parent-new", new_parent["task_id"])

    def test_fork_reconciliation_racing_done_preserves_both_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, home = Path(tmp) / "sessions", Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            parent, child = str(uuid.uuid4()), str(uuid.uuid4())
            register_job(parent, repo, "old", home, task_id="parent-old", start_watchdog=False)
            self.write_rollout(sessions, parent, [
                line("session_meta", {"id": parent, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "parent-old", "started_at": 100}),
                line("event_msg", {"type": "user_message", "message": "old"}),
            ])
            child_job = register_job(child, repo, "child", home, task_id="child-turn",
                                     parent_thread_id=parent, parent_task_id="wrong",
                                     root_thread_id=parent, fork_timestamp=150,
                                     association_source="heuristic_active", start_watchdog=False)
            child_path = home / "auto-resume" / "jobs" / f"{child_job['job_id']}.json"
            entered, release, failures = threading.Event(), threading.Event(), []
            from auto_resume import registering
            original_merge = registering.merge_registration

            def paused_merge(existing, incoming):
                result = original_merge(existing, incoming)
                entered.set(); release.wait(5)
                return result

            def reconcile():
                try:
                    register_job(child, repo, "child", home, task_id="child-turn",
                                 parent_thread_id=parent, parent_task_id="parent-old",
                                 root_thread_id=parent, fork_timestamp=150,
                                 association_source="fork_timestamp", start_watchdog=False)
                except BaseException as exc:
                    failures.append(exc)

            def complete():
                try:
                    entered.wait(5)
                    update_job(child_path, lambda value: value.update(status="DONE"))
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch("auto_resume.registering.merge_registration", side_effect=paused_merge):
                first, second = threading.Thread(target=reconcile), threading.Thread(target=complete)
                first.start(); second.start(); self.assertTrue(entered.wait(5)); release.set()
                first.join(timeout=5); second.join(timeout=5)
            self.assertFalse(failures)
            final = load_job(child_path)
            self.assertEqual("DONE", final["status"])
            self.assertEqual("parent-old", final["parent_task_id"])
            self.assertEqual("fork_timestamp", final["association_source"])

    def test_concurrent_preflight_and_daemon_share_one_watchdog_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            original = register_job(thread, repo, "goal", home, task_id="turn-a",
                                    start_watchdog=False)
            started, calls, failures, outcomes = threading.Event(), [], [], []

            def fake_live(*_args, **_kwargs):
                return started.is_set()

            def fake_launch(job_path, **_kwargs):
                calls.append(Path(job_path).resolve())
                time.sleep(0.1)
                started.set()
                return 123

            barrier = threading.Barrier(2)

            def run_preflight():
                try:
                    barrier.wait(5)
                    outcomes.append(preflight(thread, repo, "goal", home, task_id="turn-a",
                                              start_watchdog=True))
                except BaseException as exc:
                    failures.append(exc)

            def run_daemon():
                try:
                    barrier.wait(5)
                    outcomes.append(scan_once(home))
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}, clear=False), \
                    mock.patch("auto_resume.registering.watchdog_lease_is_live", side_effect=fake_live), \
                    mock.patch("auto_resume.registering.launch_watchdog", side_effect=fake_launch), \
                    mock.patch("auto_resume.daemon.ensure_daemon_started",
                               return_value=({"running": True}, False)):
                first = threading.Thread(target=run_preflight)
                second = threading.Thread(target=run_daemon)
                first.start(); second.start(); first.join(5); second.join(5)
            self.assertFalse(failures)
            self.assertEqual(1, len(calls))
            jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")]
            self.assertEqual([original["job_id"]], [job["job_id"] for job in jobs])
            preflight_result = next(value for value in outcomes if value.get("job"))
            self.assertEqual(original["job_id"], preflight_result["job"]["job_id"])

    def test_opt_out_tombstone_survives_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"; repo.mkdir(); self.make_repo(repo)
            thread = str(uuid.uuid4())
            self.write_rollout(sessions, thread, [
                line("session_meta", {"id": thread, "cwd": str(repo)}),
                line("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
                line("event_msg", {"type": "user_message", "message": "goal"}),
            ])
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}, clear=False):
                skipped = preflight(thread, repo, "goal", home, opt_out=True, task_id="turn-a",
                                    sessions_root=sessions, start_watchdog=False)
            self.assertEqual("SKIPPED", skipped["outcome"])
            result = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, result["ignored"])
            self.assertEqual([], list((home / "auto-resume" / "jobs").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
