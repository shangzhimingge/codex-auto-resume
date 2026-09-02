import signal
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


class ProcessTreeTests(unittest.TestCase):
    def test_windows_uses_hidden_taskkill_tree_then_process_fallback(self):
        proc = mock.Mock(pid=1234)
        proc.wait.side_effect = [subprocess.TimeoutExpired("wait", 0.5), 0]
        with mock.patch.object(processes, "os", types.SimpleNamespace(name="nt")), \
                mock.patch.object(processes.subprocess, "run") as run, \
                mock.patch.object(processes, "process_identity", return_value="win:root"), \
                mock.patch.object(processes, "process_is_running", return_value=True), \
                mock.patch.object(processes.ProcessTreeGuard, "_drain_tracked"):
            processes.terminate_process_tree(proc)
        self.assertEqual(["taskkill", "/PID", "1234", "/T", "/F"],
                         run.call_args.args[0])
        kwargs = run.call_args.kwargs
        self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])
        self.assertTrue(kwargs["close_fds"])
        self.assertFalse(kwargs["shell"])
        proc.kill.assert_called_once_with()

    def test_posix_uses_process_group_term_then_kill(self):
        proc = mock.Mock(pid=4321)
        proc.wait.side_effect = [subprocess.TimeoutExpired("wait", 0.5), 0]
        fake_os = types.SimpleNamespace(name="posix", killpg=mock.Mock())
        with mock.patch.object(processes, "os", fake_os):
            processes.terminate_process_tree(proc)
        self.assertEqual([
            mock.call(4321, signal.SIGTERM),
            mock.call(4321, getattr(signal, "SIGKILL", 9)),
        ], fake_os.killpg.call_args_list)
        proc.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
