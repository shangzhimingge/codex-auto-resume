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
from auto_resume.processes import process_identity, process_is_running


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
        with mock.patch("auto_resume.limits.os", types.SimpleNamespace(name="nt")), \
             mock.patch("auto_resume.limits.subprocess.Popen", popen), \
             mock.patch("auto_resume.limits.terminate_process_tree") as terminate:
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
        terminate.assert_called_once_with(process)
        self.assertTrue(all(stream.closed for stream in
                            (process.stdin, process.stdout, process.stderr)))

    def test_popen_failure_is_wrapped_without_cleanup_of_an_unstarted_process(self):
        with mock.patch("auto_resume.limits.subprocess.Popen", side_effect=OSError("boom")), \
             mock.patch("auto_resume.limits.terminate_process_tree") as terminate:
            with self.assertRaisesRegex(LimitsError, "failed to start"):
                read_limits(("codex",), timeout=1)
        terminate.assert_not_called()

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
