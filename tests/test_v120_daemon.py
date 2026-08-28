import importlib
import json
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


class V120DaemonTests(unittest.TestCase):
    def make_repo(self, path):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
        Path(path, "a").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "a"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)

    def test_daemon_scan_starts_only_active_jobs_without_live_lease(self):
        daemon = importlib.import_module("auto_resume.daemon")
        from auto_resume.registering import register_job
        from auto_resume.state import save_job
        from auto_resume.watchdog_lease import lease_path
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            project = tmp / "repo"
            project.mkdir()
            self.make_repo(project)
            home = tmp / "home"
            stale = register_job(str(uuid.uuid4()), project, "goal", home, start_watchdog=False)
            stale_path = home / "auto-resume" / "jobs" / f"{stale['job_id']}.json"
            lease_path(stale_path).write_text(json.dumps({
                "pid": 999999, "process_identity": "gone", "nonce": "old",
                "heartbeat_at": time.time(), "state": "running",
            }), encoding="utf-8")
            done = register_job(str(uuid.uuid4()), project, "done", home, start_watchdog=False)
            done["status"] = "DONE"
            done_path = home / "auto-resume" / "jobs" / f"{done['job_id']}.json"
            save_job(done_path, done)
            with mock.patch("auto_resume.daemon.launch_watchdog", return_value=321) as launch:
                result = daemon.scan_once(home)
            self.assertEqual(1, result["started"])
            launch.assert_called_once_with(stale_path)

    def test_non_linux_unix_process_identity_uses_ps_fallback(self):
        processes = importlib.import_module("auto_resume.processes")
        completed = mock.Mock(returncode=0, stdout="Mon Jan  2 03:04:05 2023\n")
        with mock.patch.object(processes.os, "name", "posix"), \
             mock.patch.object(processes.Path, "exists", return_value=False), \
             mock.patch.object(processes.subprocess, "run", return_value=completed) as run:
            identity = processes.process_identity(42)
        self.assertTrue(identity.startswith("ps:"), identity)
        run.assert_called()

    def test_watchdog_launch_detaches_into_a_new_unix_session(self):
        registering = importlib.import_module("auto_resume.registering")
        with mock.patch.object(registering.os, "name", "posix"):
            self.assertEqual({"start_new_session": True}, registering.detached_process_options())


if __name__ == "__main__":
    unittest.main()
