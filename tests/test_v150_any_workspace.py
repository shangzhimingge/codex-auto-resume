import json
import hashlib
import os
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

from auto_resume.activation import ROOT_FALLBACK_GOAL, preflight
from auto_resume.daemon import scan_once
from auto_resume.registering import register_job
from auto_resume.repo import fingerprint, repo_matches
from auto_resume.state import atomic_write_json, ensure_runtime_layout, load_job
from auto_resume.workspace import Workspace, resolve_workspace
from auto_resume.session_tasks import SUBAGENT_FALLBACK_GOAL
from auto_resume.watch import _lineage_accepts


class AnyWorkspaceTests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def write_rollout(self, sessions, thread, rows):
        path = sessions / "2026" / "09" / "02" / f"rollout-v150-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps({"timestamp": "2026-09-02T00:00:00Z",
                                                "type": kind, "payload": payload})
                                  for kind, payload in rows) + "\n", encoding="utf-8")
        return path

    def test_resolver_prefers_explicit_then_actual_git_then_rollout_git_then_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            explicit = base / "explicit"; explicit.mkdir()
            actual_repo = base / "actual"; actual_repo.mkdir(); self.make_repo(actual_repo)
            actual_child = actual_repo / "nested"; actual_child.mkdir()
            rollout_repo = base / "rollout"; rollout_repo.mkdir(); self.make_repo(rollout_repo)
            rollout_child = rollout_repo / "nested"; rollout_child.mkdir()
            thread = str(uuid.uuid4())
            self.assertEqual(Workspace("directory", explicit.resolve()), resolve_workspace(
                thread, explicit=explicit, actual_cwd=actual_child, rollout_cwd=rollout_child,
                codex_home=base / "home"))
            self.assertEqual(Workspace("git", actual_repo.resolve()), resolve_workspace(
                thread, actual_cwd=actual_child, rollout_cwd=rollout_child,
                codex_home=base / "home"))
            self.assertEqual(Workspace("git", rollout_repo.resolve()), resolve_workspace(
                thread, actual_cwd=None, rollout_cwd=rollout_child, codex_home=base / "home"))
            plain = base / "plain"; plain.mkdir()
            self.assertEqual(Workspace("directory", plain.resolve()), resolve_workspace(
                thread, actual_cwd=plain, rollout_cwd=None, codex_home=base / "home"))

    def test_managed_workspace_is_final_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, thread = Path(tmp) / "home", str(uuid.uuid4())
            workspace = resolve_workspace(thread, actual_cwd=None, rollout_cwd=None, codex_home=home)
            self.assertEqual("managed", workspace.kind)
            self.assertEqual((home / "auto-resume" / "workspaces" / thread).resolve(), workspace.root)
            self.assertTrue(workspace.root.is_dir())

    def test_internal_resume_accepts_managed_workspace_as_process_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, thread = Path(tmp) / "home", str(uuid.uuid4())
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("auto_resume.activation.resolve_current_task", return_value=None):
                registered = preflight(thread, codex_home=home, task_id="turn",
                                       start_watchdog=False, actual_cwd=None)
            job = registered["job"]
            self.assertEqual("managed", job["workspace_kind"])
            internal_env = {
                "CODEX_THREAD_ID": thread,
                "CODEX_AUTO_RESUME_JOB_ID": job["job_id"],
                "CODEX_AUTO_RESUME_TASK_ID": job["task_id"],
            }
            with mock.patch.dict(os.environ, internal_env, clear=True), \
                    mock.patch("auto_resume.activation.resolve_current_task", return_value=None):
                resumed = preflight(thread, codex_home=home, start_watchdog=False,
                                    actual_cwd=Path(job["workspace_root"]))
            self.assertEqual("REUSED", resumed["outcome"])
            self.assertTrue(resumed["resume_attempt"])
            self.assertEqual(job["job_id"], resumed["job"]["job_id"])

    def test_directory_snapshot_never_recurses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"; root.mkdir()
            child = root / "child"; child.mkdir()
            (child / "payload.txt").write_text("one", encoding="utf-8")
            workspace = Workspace("directory", root.resolve())
            first = fingerprint(workspace)
            with mock.patch.object(Path, "rglob", side_effect=AssertionError("recursive scan")), \
                    mock.patch.object(Path, "iterdir", side_effect=AssertionError("directory scan")):
                second = fingerprint(workspace)
                self.assertTrue(repo_matches(workspace, second))
            (child / "payload.txt").write_text("two", encoding="utf-8")
            self.assertEqual(first, fingerprint(workspace))

    def test_root_question_without_goal_registers_in_non_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); plain = base / "plain"; plain.mkdir()
            thread = str(uuid.uuid4())
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("auto_resume.activation.resolve_current_task", return_value=None):
                result = preflight(thread, codex_home=base / "home", task_id="turn", goal=None,
                                   start_watchdog=False, actual_cwd=plain)
            self.assertEqual("REGISTERED", result["outcome"])
            self.assertEqual("directory", result["job"]["workspace_kind"])
            self.assertEqual(ROOT_FALLBACK_GOAL, result["job"]["original_goal"])

    def test_schema_v3_migrates_to_v4_without_changing_git_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); repo = base / "repo"; repo.mkdir(); self.make_repo(repo)
            home, thread = base / "home", str(uuid.uuid4())
            job = register_job(thread, repo, "goal", home, task_id="turn", start_watchdog=False)
            path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            original_id = raw["job_id"]
            for field in ("workspace_kind", "workspace_root", "expected_workspace_snapshot"):
                raw.pop(field)
            raw["schema_version"] = 3
            path.write_text(json.dumps(raw), encoding="utf-8")
            migrated = load_job(path)
            self.assertEqual(4, migrated["schema_version"])
            self.assertEqual(original_id, migrated["job_id"])
            self.assertEqual("git", migrated["workspace_kind"])
            self.assertEqual(migrated["project_root"], migrated["workspace_root"])
            self.assertEqual(migrated["expected_repo_snapshot"], migrated["expected_workspace_snapshot"])
            reused = register_job(thread, repo, "new wording", home, task_id="turn",
                                  start_watchdog=False)
            self.assertEqual(original_id, reused["job_id"])

    def test_migrated_git_job_accepts_legacy_lineage_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); repo = base / "repo"; repo.mkdir(); self.make_repo(repo)
            home, thread = base / "home", str(uuid.uuid4())
            job = register_job(thread, repo, "goal", home, task_id="turn", start_watchdog=False)
            (repo / "a.txt").write_text("child change", encoding="utf-8")
            current = fingerprint(Workspace("git", repo))
            key = hashlib.sha256(f"{thread}\0{repo.resolve()}".encode("utf-8")).hexdigest()[:24]
            path = ensure_runtime_layout(home)["state"] / f"lineage-{key}.json"
            atomic_write_json(path, {"root_thread_id": thread, "project_root": str(repo.resolve()),
                                     "job_id": "legacy-child", "snapshot": current})
            self.assertTrue(_lineage_accepts(job, home, current))

    def test_concurrent_directory_registration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / "plain"; root.mkdir(); home = base / "home"
            thread = str(uuid.uuid4()); barrier = threading.Barrier(4); results = []; errors = []
            def worker(index):
                try:
                    barrier.wait()
                    results.append(register_job(thread, root, f"goal-{index}", home,
                                                task_id="turn", start_watchdog=False))
                except Exception as exc:
                    errors.append(exc)
            workers = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(timeout=10)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual([], errors)
            self.assertEqual(1, len({job["job_id"] for job in results}))
            self.assertEqual(1, len(list((home / "auto-resume" / "jobs").glob("*.json"))))

    def test_child_without_cwd_inherits_unique_parent_workspace_and_keeps_own_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / "root"; root.mkdir()
            home = base / "home"
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, root, "parent", home, task_id="parent",
                                  start_watchdog=False)
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("auto_resume.activation.resolve_current_task", return_value={
                        "thread_id": child_thread, "task_id": "child", "cwd": None,
                        "goal": None, "parent_thread_id": parent_thread,
                        "parent_task_id": "parent", "root_thread_id": parent_thread,
                        "thread_source": "rollout", "goal_source": "agent_message",
                    }):
                result = preflight(child_thread, codex_home=home, start_watchdog=False,
                                   actual_cwd=None)
            child = result["job"]
            self.assertEqual("REGISTERED", result["outcome"])
            self.assertNotEqual(parent["job_id"], child["job_id"])
            self.assertEqual(child_thread, child["thread_id"])
            self.assertEqual("child", child["task_id"])
            self.assertEqual(parent["workspace_root"], child["workspace_root"])

    def test_explicit_child_identity_uses_child_goal_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / "root"; root.mkdir(); home = base / "home"
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            register_job(parent_thread, root, "parent", home, task_id="p", start_watchdog=False)
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("auto_resume.activation.resolve_current_task", return_value=None):
                result = preflight(child_thread, codex_home=home, task_id="c",
                                   parent_thread_id=parent_thread, parent_task_id="p",
                                   root_thread_id=parent_thread, actual_cwd=None,
                                   start_watchdog=False)
            self.assertEqual(SUBAGENT_FALLBACK_GOAL, result["job"]["original_goal"])
            self.assertEqual("subagent_fallback", result["job"]["goal_source"])

    def test_child_without_cwd_uses_managed_workspace_when_parent_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); one = base / "one"; one.mkdir(); two = base / "two"; two.mkdir()
            home, parent_thread, child_thread = base / "home", str(uuid.uuid4()), str(uuid.uuid4())
            register_job(parent_thread, one, "one", home, task_id="one", start_watchdog=False)
            register_job(parent_thread, two, "two", home, task_id="two", start_watchdog=False)
            workspace = resolve_workspace(child_thread, actual_cwd=None, rollout_cwd=None,
                                          codex_home=home, parent_thread_id=parent_thread)
            self.assertEqual("managed", workspace.kind)
            self.assertEqual((home / "auto-resume" / "workspaces" / child_thread).resolve(),
                             workspace.root)

    def test_child_with_different_workspace_stays_linked_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); parent_root = base / "parent"; parent_root.mkdir()
            child_root = base / "child"; child_root.mkdir()
            home = base / "home"
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            register_job(parent_thread, parent_root, "parent", home, task_id="p", start_watchdog=False)
            child = register_job(child_thread, child_root, "child", home, task_id="c",
                                 parent_thread_id=parent_thread, parent_task_id="p",
                                 root_thread_id=parent_thread, start_watchdog=False)
            self.assertEqual(str(child_root.resolve()), child["workspace_root"])
            self.assertEqual(parent_thread, child["parent_thread_id"])
            self.assertEqual("p", child["parent_task_id"])

    def test_daemon_discovers_non_git_root_and_cross_workspace_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); sessions = base / "sessions"; home = base / "home"
            parent_root = base / "parent"; parent_root.mkdir()
            child_root = base / "child"; child_root.mkdir()
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            self.write_rollout(sessions, parent_thread, [
                ("session_meta", {"id": parent_thread, "cwd": str(parent_root)}),
                ("event_msg", {"type": "task_started", "turn_id": "p"}),
                ("event_msg", {"type": "user_message", "message": "parent"}),
            ])
            self.write_rollout(sessions, child_thread, [
                ("session_meta", {"id": child_thread, "cwd": str(child_root),
                                  "parent_thread_id": parent_thread, "parent_task_id": "p"}),
                ("event_msg", {"type": "task_started", "turn_id": "c"}),
                ("event_msg", {"type": "agent_message", "message": "child"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                result = scan_once(home, sessions_root=sessions)
            self.assertEqual(2, result["registered"])
            jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")]
            parent = next(job for job in jobs if job["thread_id"] == parent_thread)
            child = next(job for job in jobs if job["thread_id"] == child_thread)
            self.assertEqual("directory", parent["workspace_kind"])
            self.assertEqual(str(child_root.resolve()), child["workspace_root"])
            self.assertEqual(parent["task_id"], child["parent_task_id"])
            self.assertNotEqual(parent["workspace_root"], child["workspace_root"])

    def test_daemon_child_without_cwd_inherits_the_unique_parent_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); sessions = base / "sessions"; home = base / "home"
            parent_root = base / "parent"; parent_root.mkdir()
            parent_thread, child_thread = str(uuid.uuid4()), str(uuid.uuid4())
            parent = register_job(parent_thread, parent_root, "parent", home, task_id="p",
                                  start_watchdog=False)
            self.write_rollout(sessions, child_thread, [
                ("session_meta", {"id": child_thread, "parent_thread_id": parent_thread,
                                  "parent_task_id": "p"}),
                ("event_msg", {"type": "task_started", "turn_id": "c"}),
                ("event_msg", {"type": "agent_message", "message": "child"}),
            ])
            with mock.patch("auto_resume.daemon.ensure_watchdog_started",
                            side_effect=lambda path: (load_job(path), False)):
                result = scan_once(home, sessions_root=sessions)
            self.assertEqual(1, result["registered"])
            jobs = [load_job(path) for path in (home / "auto-resume" / "jobs").glob("*.json")]
            child = next(job for job in jobs if job["thread_id"] == child_thread)
            self.assertEqual(parent["workspace_root"], child["workspace_root"])
            self.assertEqual(parent["workspace_kind"], child["workspace_kind"])


if __name__ == "__main__":
    unittest.main()
