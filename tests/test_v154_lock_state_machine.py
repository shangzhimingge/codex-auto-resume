import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_resume.processes import process_identity, process_owner_state
from auto_resume.state import FileLock, OwnerState, RecoveryState


def _worker_once(path, start, queue):
    try:
        start.wait(10)
        with FileLock(path, timeout=10):
            owner = json.loads(Path(path).read_text(encoding="utf-8"))
            queue.put(("entered", owner["nonce"]))
            time.sleep(0.05)
    except BaseException as exc:
        queue.put(("error", repr(exc)))


def _stress_worker(lock_path, counter_path, loops, start, queue):
    try:
        start.wait(10)
        for _ in range(loops):
            with FileLock(lock_path, timeout=15, poll_interval=0.005):
                path = Path(counter_path)
                value = int(path.read_text(encoding="ascii"))
                time.sleep(0.002)
                path.write_text(str(value + 1), encoding="ascii")
        queue.put(("ok", os.getpid()))
    except BaseException as exc:
        queue.put(("error", repr(exc)))


class LockRecoveryStateMachineTests(unittest.TestCase):
    def stale(self, path, nonce="old"):
        data = json.dumps({
            "pid": 999999999,
            "process_identity": "gone",
            "nonce": nonce,
            "created_at": 0,
        }).encode("utf-8")
        path.write_bytes(data)
        return data

    def absent_owner(self):
        return mock.patch("auto_resume.state.process_owner_state", return_value="absent")

    def test_permission_probe_disappears_before_read_retries_at_timeout_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.lock"
            self.stale(path)
            guard = FileLock(path, timeout=0)
            real_create = guard._create_lock_file
            real_read = guard._read_snapshot
            creates = 0
            reads = 0

            def create():
                nonlocal creates
                creates += 1
                if creates == 1:
                    raise PermissionError("existing-lock shape")
                return real_create()

            def read():
                nonlocal reads
                reads += 1
                if reads == 1:
                    path.unlink()
                return real_read()

            with mock.patch.object(guard, "_create_lock_file", side_effect=create), \
                    mock.patch.object(guard, "_read_snapshot", side_effect=read):
                with guard:
                    self.assertTrue(path.exists())
            self.assertEqual(2, creates)
            self.assertFalse(path.exists())

    def test_permission_probe_succeeds_then_recovery_first_read_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recover-first.lock"
            self.stale(path)
            guard = FileLock(path, timeout=0)
            real_create = guard._create_lock_file
            real_read = guard._read_snapshot
            creates = 0
            reads = 0

            def create():
                nonlocal creates
                creates += 1
                if creates == 1:
                    raise PermissionError("existing-lock shape")
                return real_create()

            def read():
                nonlocal reads
                reads += 1
                if reads == 2:
                    path.unlink()
                return real_read()

            with mock.patch.object(guard, "_create_lock_file", side_effect=create), \
                    mock.patch.object(guard, "_read_snapshot", side_effect=read):
                with guard:
                    self.assertTrue(path.exists())
                    self.assertEqual(2, reads)
            self.assertFalse(path.exists())

    def test_recovery_first_read_disappears_after_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exists-first.lock"
            self.stale(path)
            guard = FileLock(path, timeout=0)
            real_read = guard._read_snapshot
            reads = 0

            def read():
                nonlocal reads
                reads += 1
                if reads == 1:
                    path.unlink()
                return real_read()

            with mock.patch.object(guard, "_read_snapshot", side_effect=read):
                with guard:
                    self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_lock_disappears_before_compare_and_rebuilds_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compare.lock"
            self.stale(path)
            guard = FileLock(path, timeout=0)
            real_read = guard._read_snapshot
            reads = 0

            def read():
                nonlocal reads
                reads += 1
                if reads == 2:
                    path.unlink()
                return real_read()

            with self.absent_owner(), mock.patch.object(
                    guard, "_read_snapshot", side_effect=read):
                with guard:
                    self.assertNotEqual("old", json.loads(path.read_text())["nonce"])
            self.assertFalse(path.exists())

    def test_new_owner_replacement_before_unlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replace.lock"
            self.stale(path)
            guard = FileLock(path, timeout=0)
            real_read = guard._read_snapshot
            reads = 0
            replacement = json.dumps({
                "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
                "nonce": "new-owner", "created_at": time.time(),
            }).encode("utf-8")

            def read():
                nonlocal reads
                reads += 1
                if reads == 3:
                    path.unlink()
                    path.write_bytes(replacement)
                return real_read()

            with self.absent_owner(), mock.patch.object(
                    guard, "_read_snapshot", side_effect=read):
                with self.assertRaises(RuntimeError):
                    guard.__enter__()
            self.assertEqual(replacement, path.read_bytes())

    def test_same_content_replacement_is_detected_by_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity.lock"
            stale = self.stale(path)
            guard = FileLock(path, timeout=0)
            real_read = guard._read_snapshot
            reads = 0

            def read():
                nonlocal reads
                reads += 1
                if reads == 3:
                    path.unlink()
                    path.write_bytes(stale)
                return real_read()

            with self.absent_owner(), mock.patch.object(
                    guard, "_read_snapshot", side_effect=read):
                with self.assertRaises(RuntimeError):
                    guard.__enter__()
            self.assertEqual(stale, path.read_bytes())

    def test_lock_disappears_during_unlink_and_rebuilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unlink-race.lock"
            stale = self.stale(path)
            guard = FileLock(path, timeout=0)
            real_unlink = Path.unlink
            injected = False

            def unlink(candidate, *args, **kwargs):
                nonlocal injected
                if candidate == path and not injected and candidate.read_bytes() == stale:
                    injected = True
                    os.unlink(candidate)
                    raise FileNotFoundError(candidate)
                return real_unlink(candidate, *args, **kwargs)

            with self.absent_owner(), mock.patch.object(Path, "unlink", new=unlink):
                with guard:
                    self.assertTrue(path.exists())
            self.assertTrue(injected)
            self.assertFalse(path.exists())

    def test_stale_unlink_success_is_followed_by_rebuild_under_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rebuild.lock"
            self.stale(path)
            with self.absent_owner(), FileLock(path, timeout=0):
                self.assertNotEqual("old", json.loads(path.read_text())["nonce"])
            self.assertFalse(path.exists())

    def test_unknown_identity_and_probe_exception_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown.lock"
            original = self.stale(path)
            with mock.patch("auto_resume.state.process_owner_state",
                            return_value="unknown_or_identity_mismatch"):
                with self.assertRaises(RuntimeError):
                    FileLock(path, timeout=0).__enter__()
            self.assertEqual(original, path.read_bytes())
            with mock.patch("auto_resume.state.process_owner_state",
                            side_effect=PermissionError("identity denied")):
                with self.assertRaises(RuntimeError):
                    FileLock(path, timeout=0).__enter__()
            self.assertEqual(original, path.read_bytes())

    def test_unreadable_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unreadable.lock"
            original = self.stale(path)
            guard = FileLock(path, timeout=0)
            denied = PermissionError("unreadable")
            with mock.patch.object(
                    guard, "_read_snapshot",
                    return_value=(RecoveryState.INACCESSIBLE, None, denied)):
                with self.assertRaises(PermissionError):
                    guard.__enter__()
            self.assertEqual(original, path.read_bytes())

    def test_owner_state_three_way_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owner.lock"
            guard = FileLock(path)
            self.stale(path)
            state, snapshot, _ = guard._read_snapshot()
            self.assertIsNone(state)
            for value, expected in (
                    ("absent", OwnerState.ABSENT),
                    ("live_match", OwnerState.LIVE_MATCH),
                    ("unknown_or_identity_mismatch", OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH)):
                with mock.patch("auto_resume.state.process_owner_state", return_value=value):
                    self.assertIs(expected, guard._owner_state(snapshot))

    def test_process_owner_current_identity_and_mismatch(self):
        identity = process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual("live_match", process_owner_state(os.getpid(), identity))
        self.assertEqual("unknown_or_identity_mismatch",
                         process_owner_state(os.getpid(), identity + "-mismatch"))

    def test_two_recoverers_serialize_and_preserve_each_new_nonce(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "two.lock"
            self.stale(path)
            ctx = multiprocessing.get_context("spawn")
            start, queue = ctx.Event(), ctx.Queue()
            workers = [ctx.Process(target=_worker_once, args=(str(path), start, queue))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            start.set()
            results = [queue.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(20)
                self.assertEqual(0, worker.exitcode)
            self.assertTrue(all(kind == "entered" for kind, _ in results), results)
            self.assertEqual(2, len({nonce for _, nonce in results}))
            self.assertFalse(path.exists())

    def test_real_multiprocess_gate_stress(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "stress.lock"
            counter = Path(tmp) / "counter.txt"
            counter.write_text("0", encoding="ascii")
            ctx = multiprocessing.get_context("spawn")
            start, queue = ctx.Event(), ctx.Queue()
            workers = [ctx.Process(
                target=_stress_worker, args=(str(lock), str(counter), 12, start, queue))
                for _ in range(4)]
            for worker in workers:
                worker.start()
            start.set()
            results = [queue.get(timeout=60) for _ in workers]
            for worker in workers:
                worker.join(20)
                self.assertEqual(0, worker.exitcode)
            self.assertTrue(all(kind == "ok" for kind, _ in results), results)
            self.assertEqual(48, int(counter.read_text(encoding="ascii")))
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
