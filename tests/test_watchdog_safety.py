import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.processes import process_identity, process_is_running
from auto_resume.registering import launch_watchdog, WatchdogStartError
from auto_resume.state import FileLock, load_job
from auto_resume.watchdog_lease import lease_path, watchdog_lease_is_live


class WatchdogSafetyTests(unittest.TestCase):
    def terminate_test_process(self, pid):
        identity = process_identity(pid)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        deadline = time.monotonic() + 5
        while process_is_running(pid, identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        if process_is_running(pid, identity):
            try:
                force_signal = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
                os.kill(pid, force_signal)
            except ProcessLookupError:
                pass

    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def test_pid_reuse_identity_is_rejected(self):
        pid = os.getpid()
        identity = process_identity(pid)
        self.assertTrue(process_is_running(pid, identity))
        self.assertFalse(process_is_running(pid, identity + "-reused"))
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "job.json"
            lease_path(job_path).write_text(json.dumps({
                "pid": pid, "process_identity": identity + "-reused", "nonce": "n",
                "heartbeat_at": time.time(), "state": "running",
            }), encoding="utf-8")
            self.assertFalse(watchdog_lease_is_live(job_path, pid, stale_after=30))
            lease_path(job_path).write_text(json.dumps({
                "pid": pid, "process_identity": None, "nonce": "n",
                "heartbeat_at": time.time(), "state": "running",
            }), encoding="utf-8")
            self.assertFalse(watchdog_lease_is_live(job_path, pid, stale_after=30))

    def test_launcher_requires_child_to_persist_verified_pid_before_success(self):
        from auto_resume.registering import register_job
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            job = register_job(str(uuid.uuid4()), project, "goal", home, start_watchdog=False)
            job_path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            with mock.patch("auto_resume.registering.subprocess.Popen", return_value=process), \
                 mock.patch("auto_resume.registering.read_lease", return_value={"nonce": "nonce", "pid": 1234}), \
                 mock.patch("auto_resume.registering.watchdog_lease_is_live", return_value=True), \
                 mock.patch("auto_resume.registering.uuid.uuid4", return_value=mock.Mock(hex="nonce")), \
                 mock.patch("auto_resume.registering._terminate_process_tree"):
                with self.assertRaises(WatchdogStartError):
                    launch_watchdog(job_path, handshake_timeout=0.2)
            self.assertIsNone(load_job(job_path)["watchdog_pid"])

    def test_stale_crash_lock_is_recovered_after_owner_is_proven_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "crash.lock"
            code = (
                "import os,sys;sys.path.insert(0,sys.argv[2]);"
                "from auto_resume.state import FileLock;"
                "guard=FileLock(sys.argv[1]);guard.__enter__();print('LOCKED',flush=True);os._exit(0)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, str(lock), str(SCRIPTS)],
                                     stdout=subprocess.PIPE, text=True, shell=False)
            self.assertEqual("LOCKED", child.stdout.readline().strip())
            child.stdout.close()
            self.assertEqual(0, child.wait(timeout=5))
            self.assertTrue(lock.exists())
            with FileLock(lock, timeout=1):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

    def test_concurrent_live_lock_owner_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "live.lock"
            with FileLock(lock):
                original = lock.read_bytes()
                with self.assertRaises(RuntimeError):
                    with FileLock(lock, timeout=0.1):
                        pass
                self.assertEqual(original, lock.read_bytes())

    def test_real_watchdog_startup_handshake_persists_verified_pid(self):
        from auto_resume.registering import register_job
        fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            job = register_job(str(uuid.uuid4()), project, "goal", home,
                               poll_interval_seconds=30, start_watchdog=False)
            job_path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            previous = os.environ.get("FAKE_LIMITS")
            os.environ["FAKE_LIMITS"] = '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}'
            try:
                pid = launch_watchdog(job_path, codex_command=[sys.executable, str(fake)], handshake_timeout=5)
                saved = load_job(job_path)
                self.assertEqual(pid, saved["watchdog_pid"])
                self.assertTrue(watchdog_lease_is_live(job_path, pid, stale_after=60))
            finally:
                if previous is None:
                    os.environ.pop("FAKE_LIMITS", None)
                else:
                    os.environ["FAKE_LIMITS"] = previous
                if 'pid' in locals() and process_is_running(pid):
                    self.terminate_test_process(pid)
                    lock = job_path.with_suffix(".lock")
                    with FileLock(lock, timeout=5):
                        pass

    def test_real_stale_watchdog_restart_and_failed_handshake_never_persist_unverified_pid(self):
        from auto_resume.registering import register_job
        from auto_resume.state import save_job
        fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.make_repo(project)
            home = Path(tmp) / "home"
            thread_id = str(uuid.uuid4())
            job = register_job(thread_id, project, "goal", home, start_watchdog=False)
            job_path = home / "auto-resume" / "jobs" / f"{job['job_id']}.json"
            job["watchdog_pid"] = os.getpid()
            save_job(job_path, job)
            lease_path(job_path).write_text(json.dumps({
                "pid": os.getpid(), "process_identity": process_identity(os.getpid()) + "-old",
                "nonce": "old", "heartbeat_at": time.time(), "state": "running",
            }), encoding="utf-8")
            old = os.environ.get("FAKE_LIMITS")
            os.environ["FAKE_LIMITS"] = '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}'
            restarted_pid = None
            try:
                restarted = register_job(
                    thread_id, project, "different wording", home, start_watchdog=True,
                    watchdog_codex_command=[sys.executable, str(fake)])
                restarted_pid = restarted["watchdog_pid"]
                self.assertNotEqual(os.getpid(), restarted_pid)
                self.assertTrue(watchdog_lease_is_live(job_path, restarted_pid, stale_after=60))
            finally:
                if old is None:
                    os.environ.pop("FAKE_LIMITS", None)
                else:
                    os.environ["FAKE_LIMITS"] = old
                if restarted_pid and process_is_running(restarted_pid):
                    self.terminate_test_process(restarted_pid)
                    with FileLock(job_path.with_suffix(".lock"), timeout=5):
                        pass

            job["watchdog_pid"] = None
            save_job(job_path, job)
            with FileLock(job_path.with_suffix(".lock")):
                with self.assertRaises(WatchdogStartError):
                    launch_watchdog(job_path, codex_command=[sys.executable, str(fake)], handshake_timeout=1)
            self.assertIsNone(load_job(job_path)["watchdog_pid"])


if __name__ == "__main__":
    unittest.main()
