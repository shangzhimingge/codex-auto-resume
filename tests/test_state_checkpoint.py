import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
