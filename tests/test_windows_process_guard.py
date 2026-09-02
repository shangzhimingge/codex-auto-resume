import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume import processes


class WindowsProcessTreeGuardTests(unittest.TestCase):
    def make_guard(self):
        guard = processes.ProcessTreeGuard(platform_name="nt", drain_timeout=0.05, poll_interval=0)
        guard._job_handle = 99
        return guard

    def test_cleanup_terminates_job_then_fallback_then_waits_and_closes(self):
        proc = mock.Mock(pid=1234)
        proc._handle = 4321
        proc.wait.return_value = 0
        guard = self.make_guard()
        guard._proc = proc
        guard._root_pid = proc.pid
        guard._tracked = {1234: "root", 2345: "child"}
        events = []
        with mock.patch.object(guard, "_snapshot_descendants",
                               side_effect=lambda: events.append("snapshot")), \
             mock.patch.object(guard, "_terminate_job",
                               side_effect=lambda: events.append("job")), \
             mock.patch.object(guard, "_taskkill_tree",
                               side_effect=lambda: events.append("taskkill")), \
             mock.patch.object(guard, "_terminate_tracked",
                               side_effect=lambda: events.append("terminate")), \
             mock.patch.object(guard, "_drain_tracked",
                               side_effect=lambda: events.append("drain")), \
             mock.patch.object(guard, "_close_job",
                               side_effect=lambda: events.append("close")):
            guard.close()
        self.assertEqual(
            ["snapshot", "job", "taskkill", "terminate", "drain", "close"], events)
        proc.wait.assert_called_once()

    def test_attach_assign_failure_keeps_identity_fallback(self):
        proc = mock.Mock(pid=1234)
        proc._handle = 4321
        guard = self.make_guard()
        with mock.patch.object(processes, "process_identity", return_value="win:root"), \
             mock.patch.object(guard, "_assign_job", side_effect=OSError("assign failed")), \
             mock.patch.object(guard, "_snapshot_descendants"):
            guard.attach(proc)
        self.assertEqual({1234: "win:root"}, guard._tracked)
        self.assertIsNotNone(guard.assignment_error)

    def test_pid_reuse_is_not_terminated(self):
        guard = self.make_guard()
        guard._tracked = {2345: "win:old"}
        with mock.patch.object(processes, "process_identity", return_value="win:new"), \
             mock.patch.object(processes, "_terminate_process") as terminate:
            guard._terminate_tracked()
        terminate.assert_not_called()

    def test_drain_timeout_reports_identity_bound_survivors(self):
        guard = self.make_guard()
        guard._tracked = {2345: "win:child"}
        with mock.patch.object(processes, "process_is_running", return_value=True):
            with self.assertRaisesRegex(processes.ProcessCleanupError, "2345"):
                guard._drain_tracked()

    def test_posix_guard_uses_existing_group_cleanup(self):
        proc = mock.Mock(pid=4321)
        guard = processes.ProcessTreeGuard(platform_name="posix")
        guard.attach(proc)
        with mock.patch.object(processes, "terminate_process_tree") as terminate:
            guard.close()
        terminate.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()

