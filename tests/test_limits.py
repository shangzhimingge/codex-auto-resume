import os
import io
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.limits import LimitsError, read_limits, reset_deadline, _normalize_command
from auto_resume.processes import (
    ProcessCleanupError, ProcessTreeGuard, process_identity, process_is_running,
)


class LimitsTests(unittest.TestCase):
    def limits(self, value):
        with mock.patch.dict(os.environ, {"FAKE_LIMITS": value}, clear=False):
            return read_limits(self.command, timeout=3)

    def test_prefers_codex_limit_id_and_waits_for_latest_exhausted_bucket(self):
        value = ('{"rateLimits":{"primary":{"usedPercent":1,"resetsAt":10}},'
                 '"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":100,"resetsAt":20},'
                 '"secondary":{"usedPercent":100,"resetsAt":30}}}}')
        snapshot = self.limits(value)
        self.assertEqual("codex", snapshot.limit_id)
        self.assertEqual(30, reset_deadline(snapshot, now=5))

    def test_falls_back_to_legacy_rate_limits(self):
        snapshot = self.limits('{"rateLimits":{"primary":{"usedPercent":100,"resetsAt":40}}}')
        self.assertEqual("legacy", snapshot.limit_id)
        self.assertEqual(40, reset_deadline(snapshot, now=5))

    def test_non_exhausted_has_no_deadline(self):
        snapshot = self.limits('{"rateLimits":{"primary":{"usedPercent":99,"resetsAt":40}}}')
        self.assertIsNone(reset_deadline(snapshot, now=5))

    def test_malformed_response_fails_closed(self):
        with self.assertRaises(LimitsError):
            self.limits('{"rateLimits":{"primary":{"usedPercent":"all","resetsAt":40}}}')

    def test_credits_are_ignored(self):
        value = ('{"rateLimits":{"primary":{"usedPercent":100,"resetsAt":40}},'
                 '"credits":{"hasCredits":true,"balance":"999","creditId":"paid"}}')
        snapshot = self.limits(value)
        self.assertEqual(40, reset_deadline(snapshot, now=5))

    def test_non_null_reached_type_counts_as_exhausted(self):
        value = ('{"rateLimits":{"primary":{"usedPercent":10,"resetsAt":40},'
                 '"rateLimitReachedType":"primary"}}')
        snapshot = self.limits(value)
        self.assertEqual(40, reset_deadline(snapshot, now=5))

    def test_past_reset_timestamp_requests_reprobe_instead_of_error(self):
        snapshot = self.limits('{"rateLimits":{"primary":{"usedPercent":100,"resetsAt":4}}}')
        self.assertEqual(5, reset_deadline(snapshot, now=5))

    def test_windows_default_uses_node_entrypoint_not_batch_wrapper(self):
        wrapper = Path(self._tmp.name) / "codex.cmd"
        entry = wrapper.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        def locate(name):
            return str(wrapper) if name == "codex.cmd" else r"C:\\Node\\node.exe"
        with mock.patch("auto_resume.limits.os", types.SimpleNamespace(name="nt")), \
             mock.patch("auto_resume.limits.shutil.which", side_effect=locate):
            self.assertEqual([r"C:\\Node\\node.exe", str(entry)], _normalize_command(("codex",)))

    def test_rpc_handshake_order_and_request_ids(self):
        rpc_log = Path(self._tmp.name) / "rpc.json"
        value = '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}'
        with mock.patch.dict(os.environ, {"FAKE_LIMITS": value, "FAKE_RPC_LOG": str(rpc_log)}, clear=False):
            read_limits(self.command, timeout=3)
        messages = json.loads(rpc_log.read_text(encoding="utf-8"))
        self.assertEqual(["initialize", "initialized", "account/rateLimits/read"],
                         [message["method"] for message in messages])
        self.assertEqual(1, messages[0]["id"])
        self.assertNotIn("id", messages[1])
        self.assertEqual(2, messages[2]["id"])

    def test_windows_probe_is_hidden_detached_from_terminal_and_always_cleaned(self):
        responses = io.StringIO(
            '{"id":1,"result":{"userAgent":"fake"}}\n'
            '{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}}\n'
        )
        process = mock.Mock(pid=123, stdin=io.StringIO(), stdout=responses,
                            stderr=io.StringIO())
        popen = mock.Mock(return_value=process)
        guard = mock.Mock()
        with mock.patch("auto_resume.limits.os", types.SimpleNamespace(name="nt")), \
             mock.patch("auto_resume.limits.subprocess.Popen", popen), \
             mock.patch("auto_resume.limits.ProcessTreeGuard", return_value=guard):
            snapshot = read_limits(("codex.exe",), timeout=1)
        self.assertEqual("legacy", snapshot.limit_id)
        kwargs = popen.call_args.kwargs
        expected = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        self.assertEqual(expected, kwargs["creationflags"])
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        self.assertIs(subprocess.PIPE, kwargs["stdin"])
        self.assertIs(subprocess.PIPE, kwargs["stdout"])
        self.assertIs(subprocess.PIPE, kwargs["stderr"])
        guard.attach.assert_called_once_with(process)
        guard.close.assert_called_once_with()
        self.assertTrue(all(stream.closed for stream in
                            (process.stdin, process.stdout, process.stderr)))

    def test_popen_failure_is_wrapped_without_cleanup_of_an_unstarted_process(self):
        with mock.patch("auto_resume.limits.subprocess.Popen", side_effect=OSError("boom")), \
             mock.patch("auto_resume.limits.ProcessTreeGuard") as guard_type:
            with self.assertRaisesRegex(LimitsError, "failed to start"):
                read_limits(("codex",), timeout=1)
        guard_type.return_value.attach.assert_not_called()
        guard_type.return_value.close.assert_called_once_with()

    def test_cleanup_error_does_not_mask_rpc_error(self):
        responses = io.StringIO('{"id":1,"result":{}}\n{malformed\n')
        process = mock.Mock(pid=123, stdin=io.StringIO(), stdout=responses,
                            stderr=io.StringIO())
        guard = mock.Mock()
        guard.close.side_effect = ProcessCleanupError("cleanup failed")
        with mock.patch("auto_resume.limits.subprocess.Popen", return_value=process), \
             mock.patch("auto_resume.limits.ProcessTreeGuard", return_value=guard):
            with self.assertRaisesRegex(LimitsError, "malformed app-server JSON"):
                read_limits(("codex",), timeout=1)

    def test_cleanup_error_after_valid_rpc_is_reported(self):
        responses = io.StringIO(
            '{"id":1,"result":{}}\n'
            '{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}}\n'
        )
        process = mock.Mock(pid=123, stdin=io.StringIO(), stdout=responses,
                            stderr=io.StringIO())
        guard = mock.Mock()
        guard.close.side_effect = ProcessCleanupError("cleanup failed")
        with mock.patch("auto_resume.limits.subprocess.Popen", return_value=process), \
             mock.patch("auto_resume.limits.ProcessTreeGuard", return_value=guard):
            with self.assertRaisesRegex(LimitsError, "cleanup failed"):
                read_limits(("codex",), timeout=1)

    def test_success_malformed_and_timeout_reap_the_entire_fixture_tree(self):
        value = '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}'
        for mode in ("success", "malformed", "hang"):
            with self.subTest(mode=mode):
                record = Path(self._tmp.name) / f"{mode}-processes.json"
                env = {
                    "FAKE_LIMITS": value,
                    "FAKE_APP_SERVER_MODE": mode,
                    "FAKE_APP_SERVER_PROCESS_RECORD": str(record),
                    "FAKE_AUTO_RESUME_SCRIPTS": str(SCRIPTS),
                }
                error = None
                try:
                    with mock.patch.dict(os.environ, env, clear=False):
                        if mode == "success":
                            read_limits(self.command, timeout=1)
                        else:
                            with self.assertRaises(LimitsError):
                                read_limits(self.command, timeout=0.2)
                except BaseException as exc:
                    error = exc
                finally:
                    deadline = time.monotonic() + 3
                    while not record.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    records = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
                    leftovers = [item for item in records
                                 if process_is_running(item["pid"], item.get("identity"))]
                    for item in leftovers:
                        self._force_cleanup(item["pid"], item.get("identity"))
                if error is not None:
                    raise error
                self.assertEqual(2, len(records), records)
                self.assertEqual([], leftovers, records)

    def test_child_spawned_on_either_side_of_job_attach_is_drained(self):
        value = '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}'
        original_attach = ProcessTreeGuard.attach
        for timing in ("before_attach", "after_attach"):
            with self.subTest(timing=timing):
                record = Path(self._tmp.name) / f"{timing}-processes.json"
                ready = Path(self._tmp.name) / f"{timing}-ready"
                release = Path(self._tmp.name) / f"{timing}-release"
                assignment_errors = []

                def barrier_attach(guard, proc):
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "fixture did not reach child barrier")
                    if timing == "before_attach":
                        result = original_attach(guard, proc)
                    else:
                        result = original_attach(guard, proc)
                        release.write_text("go", encoding="utf-8")
                    assignment_errors.append(guard.assignment_error)
                    return result

                env = {
                    "FAKE_LIMITS": value,
                    "FAKE_APP_SERVER_PROCESS_RECORD": str(record),
                    "FAKE_APP_SERVER_CHILD_TIMING": timing,
                    "FAKE_APP_SERVER_CHILD_READY": str(ready),
                    "FAKE_APP_SERVER_CHILD_RELEASE": str(release),
                    "FAKE_AUTO_RESUME_SCRIPTS": str(SCRIPTS),
                }
                with mock.patch.dict(os.environ, env, clear=False), \
                     mock.patch.object(ProcessTreeGuard, "attach", barrier_attach):
                    read_limits(self.command, timeout=3)
                records = json.loads(record.read_text(encoding="utf-8"))
                leftovers = [
                    item for item in records
                    if process_is_running(item["pid"], item.get("identity"))
                ]
                for item in leftovers:
                    self._force_cleanup(item["pid"], item.get("identity"))
                if os.name == "nt":
                    self.assertEqual([None], assignment_errors)
                self.assertEqual([], leftovers, records)

    @unittest.skipUnless(os.name == "nt", "Windows Job assignment fallback")
    def test_assignment_failure_drains_pre_attach_child_with_identity_fallback(self):
        record = Path(self._tmp.name) / "assign-failure-processes.json"
        ready = Path(self._tmp.name) / "assign-failure-ready"
        original_attach = ProcessTreeGuard.attach

        def barrier_attach(guard, proc):
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "fixture did not reach child barrier")
            return original_attach(guard, proc)

        env = {
            "FAKE_LIMITS": '{"rateLimits":{"primary":{"usedPercent":0,"resetsAt":null}}}',
            "FAKE_APP_SERVER_PROCESS_RECORD": str(record),
            "FAKE_APP_SERVER_CHILD_TIMING": "before_attach",
            "FAKE_APP_SERVER_CHILD_READY": str(ready),
            "FAKE_AUTO_RESUME_SCRIPTS": str(SCRIPTS),
        }
        assignment_error = OSError("forced assignment failure")
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(ProcessTreeGuard, "attach", barrier_attach), \
             mock.patch.object(ProcessTreeGuard, "_assign_job",
                               side_effect=assignment_error), \
             mock.patch.object(ProcessTreeGuard, "_assign_pid_to_job",
                               side_effect=assignment_error):
            read_limits(self.command, timeout=3)
        records = json.loads(record.read_text(encoding="utf-8"))
        leftovers = [
            item for item in records
            if process_is_running(item["pid"], item.get("identity"))
        ]
        for item in leftovers:
            self._force_cleanup(item["pid"], item.get("identity"))
        self.assertEqual([], leftovers, records)

    def _force_cleanup(self, pid, identity):
        if not process_is_running(pid, identity):
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5, check=False,
            )
        else:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 3
        while process_is_running(pid, identity) and time.monotonic() < deadline:
            time.sleep(0.02)

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
        self.command = [sys.executable, str(self.fake)]


if __name__ == "__main__":
    unittest.main()
