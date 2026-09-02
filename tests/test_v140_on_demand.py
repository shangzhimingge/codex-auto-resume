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

from auto_resume import activation, daemon, registering


class OnDemandDaemonTests(unittest.TestCase):
    def test_qualified_preflight_starts_daemon_for_registered_and_reused_jobs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(activation, "resolve_current_task", return_value=None), \
                mock.patch.object(activation, "_register_job") as register, \
                mock.patch.object(daemon, "ensure_daemon_started") as ensure:
            for outcome in ("REGISTERED", "REUSED"):
                register.return_value = ({"job_id": outcome.lower()}, outcome)
                result = activation.preflight(
                    str(uuid.uuid4()), Path(tmp), "goal", Path(tmp) / "home",
                    task_id=f"turn-{outcome}", start_watchdog=True,
                )
                self.assertEqual(outcome, result["outcome"])
            self.assertEqual(2, ensure.call_count)

    def test_skipped_opt_out_and_no_start_launch_zero_daemons(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(activation, "resolve_current_task", return_value=None), \
                mock.patch.object(activation, "_register_job", return_value=({"job_id": "job"}, "REGISTERED")), \
                mock.patch.object(daemon, "ensure_daemon_started") as ensure:
            self.assertEqual("SKIPPED", activation.preflight(codex_home=tmp)["outcome"])
            self.assertEqual("SKIPPED", activation.preflight(codex_home=tmp, opt_out=True)["outcome"])
            result = activation.preflight(
                str(uuid.uuid4()), Path(tmp), "goal", Path(tmp) / "home",
                task_id="turn", start_watchdog=False,
            )
            self.assertEqual("REGISTERED", result["outcome"])
            ensure.assert_not_called()

    def test_valid_internal_resume_merge_also_ensures_daemon(self):
        thread_id, task_id, job_id = str(uuid.uuid4()), "original-turn", "job"
        env = {"CODEX_THREAD_ID": thread_id, "CODEX_AUTO_RESUME_JOB_ID": job_id,
               "CODEX_AUTO_RESUME_TASK_ID": task_id}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"; project.mkdir()
            job = {"job_id": job_id, "thread_id": thread_id, "task_id": task_id,
                   "workspace_kind": "directory", "workspace_root": str(project),
                   "project_root": str(project)}
            with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(activation, "load_job", return_value=job), \
                mock.patch.object(activation, "ensure_runtime_layout",
                                  return_value={"jobs": Path(tmp)}), \
                mock.patch.object(activation, "resolve_current_task", return_value=None), \
                mock.patch.object(activation, "_record_resume_attempt"), \
                mock.patch.object(daemon, "ensure_daemon_started") as ensure:
                result = activation.preflight(
                    thread_id, project, codex_home=tmp, start_watchdog=True)
        self.assertEqual("REUSED", result["outcome"])
        self.assertTrue(result["resume_attempt"])
        ensure.assert_called_once_with(tmp)

    def test_launch_daemon_discards_stdio_and_uses_platform_detachment(self):
        process = mock.Mock(pid=41)
        popen = mock.Mock(return_value=process)
        with mock.patch.object(daemon, "detached_process_options",
                               return_value={"creationflags": 0x123}):
            self.assertIs(process, daemon.launch_daemon(Path("home"), popen=popen))
        kwargs = popen.call_args.kwargs
        self.assertIs(subprocess.DEVNULL, kwargs["stdin"])
        self.assertIs(subprocess.DEVNULL, kwargs["stdout"])
        self.assertIs(subprocess.DEVNULL, kwargs["stderr"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(0x123, kwargs["creationflags"])

        with mock.patch.object(registering.os, "name", "posix"):
            self.assertEqual({"start_new_session": True}, registering.detached_process_options())
        with mock.patch.object(registering.os, "name", "nt"), \
                mock.patch.object(registering.subprocess, "DETACHED_PROCESS", 1, create=True), \
                mock.patch.object(registering.subprocess, "CREATE_NEW_PROCESS_GROUP", 2, create=True), \
                mock.patch.object(registering.subprocess, "CREATE_NO_WINDOW", 4, create=True):
            self.assertEqual({"creationflags": 7}, registering.detached_process_options())

    def test_concurrent_preflights_share_one_serialized_daemon_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            launched = threading.Event()
            calls = []
            process = mock.Mock(pid=73)
            process.poll.return_value = None

            def popen(*_args, **_kwargs):
                calls.append(True)
                launched.set()
                return process

            def status(*_args, **_kwargs):
                return {"running": launched.is_set(), "pid": 73}

            barrier = threading.Barrier(2)
            results = []

            def worker():
                barrier.wait()
                results.append(daemon.ensure_daemon_started(tmp, popen=popen))

            with mock.patch.object(daemon, "daemon_status", side_effect=status), \
                    mock.patch.object(daemon, "_detach_popen"):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(1, len(calls))
            self.assertEqual([False, True], sorted(started for _state, started in results))


if __name__ == "__main__":
    unittest.main()
