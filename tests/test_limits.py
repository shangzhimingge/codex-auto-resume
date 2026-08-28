import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.limits import LimitsError, read_limits, reset_deadline, _normalize_command


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
        with mock.patch("auto_resume.limits.os.name", "nt"), \
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

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake = ROOT / "tests" / "fixtures" / "fake_codex.py"
        self.command = [sys.executable, str(self.fake)]


if __name__ == "__main__":
    unittest.main()
