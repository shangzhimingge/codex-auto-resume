import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.checkpoints import HEADINGS, read_checkpoint, write_checkpoint
from auto_resume.state import FileLock, atomic_write_json, load_json


class StateCheckpointTests(unittest.TestCase):
    def test_checkpoint_contains_required_headings_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.md"
            values = {key: f"value-{key}" for key in HEADINGS}
            write_checkpoint(path, values)
            self.assertEqual(values, read_checkpoint(path))
            text = path.read_text(encoding="utf-8")
            for heading in HEADINGS:
                self.assertIn(f"## {heading}\n", text)

    def test_atomic_json_never_leaves_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            atomic_write_json(path, {"status": "REGISTERED"})
            self.assertEqual("REGISTERED", load_json(path)["status"])
            self.assertEqual([], list(Path(tmp).glob("*.tmp")))

    def test_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with FileLock(path):
                with self.assertRaises(RuntimeError):
                    with FileLock(path):
                        pass

    def test_lock_metadata_write_failure_does_not_leak_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with mock.patch("auto_resume.state.os.write", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    with FileLock(path):
                        pass
            self.assertFalse(path.exists())
            with FileLock(path):
                pass

    def test_permission_shaped_lock_disappears_before_probe_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            real_open = os.open
            attempts = []

            def permission_then_open(candidate, flags, *args, **kwargs):
                attempts.append(Path(candidate))
                if len(attempts) == 1:
                    raise PermissionError("Windows existing-lock shape")
                return real_open(candidate, flags, *args, **kwargs)

            with mock.patch("auto_resume.state.os.open", side_effect=permission_then_open), \
                    mock.patch.object(Path, "read_bytes", side_effect=FileNotFoundError("gone")):
                with FileLock(path):
                    self.assertTrue(path.exists())

            self.assertEqual([path, path], attempts)
            self.assertFalse(path.exists())

    def test_lock_release_retries_transient_windows_file_sharing_error(self):
        lock = FileLock("job.lock", poll_interval=0)
        with mock.patch.object(Path, "unlink", side_effect=[PermissionError("busy"), None]) as unlink, \
             mock.patch("auto_resume.state.time.sleep") as sleep:
            lock._unlink_with_retry()
        self.assertEqual(2, unlink.call_count)
        sleep.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
