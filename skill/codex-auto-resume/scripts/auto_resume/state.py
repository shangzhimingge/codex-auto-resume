import json
import os
import hashlib
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path

from .processes import process_identity, process_owner_state

STATUSES = {
    "REGISTERED", "RUNNING", "WAITING_RESET", "RESUMING", "DONE",
    "NEEDS_USER", "MAX_CYCLES", "ERROR", "SUPERSEDED",
}
ACTIVE_STATES = {"REGISTERED", "RUNNING", "WAITING_RESET", "RESUMING"}
TERMINAL_STATES = {"DONE", "SUPERSEDED", "NEEDS_USER", "MAX_CYCLES", "ERROR"}
REQUIRED_JOB_FIELDS = (
    "schema_version", "job_id", "thread_id", "task_id", "thread_source",
    "parent_thread_id", "parent_task_id", "root_thread_id", "agent_path",
    "rollout_path", "goal_source", "fork_timestamp", "association_source",
    "superseded_by", "workspace_kind", "workspace_root", "project_root", "original_goal",
    "status", "billing_policy", "limit_id", "max_cycles", "completed_cycles",
    "poll_interval_seconds", "safety_margin_seconds", "checkpoint_path",
    "expected_workspace_snapshot", "expected_repo_snapshot", "watchdog_pid",
    "created_at", "updated_at", "last_error",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def runtime_home(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve() / "auto-resume"
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve() / "auto-resume"


def ensure_runtime_layout(explicit=None, best_effort=False):
    root = runtime_home(explicit)
    layout = {
        "root": root,
        "jobs": root / "jobs",
        "checkpoints": root / "checkpoints",
        "logs": root / "logs",
        "state": root / "state",
        "handoffs": root / "handoffs",
        "workspaces": root / "workspaces",
    }
    for name in ("jobs", "checkpoints", "logs", "state", "handoffs", "workspaces"):
        layout[name].mkdir(parents=True, exist_ok=True)
    migrations = {
        root / "daemon-state.json": layout["state"] / "daemon-state.json",
        root / "daemon.lock": layout["state"] / "daemon.lock",
        root / "daemon.stdout.log": layout["logs"] / "daemon.stdout.log",
        root / "daemon.stderr.log": layout["logs"] / "daemon.stderr.log",
    }
    for legacy, destination in migrations.items():
        if not legacy.exists() or destination.exists():
            continue
        try:
            os.replace(legacy, destination)
        except OSError:
            if not best_effort:
                raise
    return layout


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_job(job):
    missing = set(REQUIRED_JOB_FIELDS) - set(job)
    extra = set(job) - set(REQUIRED_JOB_FIELDS)
    if missing or extra:
        raise ValueError(f"invalid job fields: missing={sorted(missing)}, extra={sorted(extra)}")
    if job.get("schema_version") != 4:
        raise ValueError("unsupported job schema")
    if job.get("workspace_kind") not in {"git", "directory", "managed"}:
        raise ValueError("unsupported workspace kind")
    if (job.get("workspace_root") != job.get("project_root") or
            job.get("expected_workspace_snapshot") != job.get("expected_repo_snapshot")):
        raise ValueError("workspace compatibility mirrors diverged")
    maximum = job.get("max_cycles")
    if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0):
        raise ValueError("max_cycles must be null or a positive integer")
    if job["status"] not in STATUSES or job["billing_policy"] != "included_only":
        raise ValueError("invalid job policy or status")
    return job


def migrate_job(job):
    migrated = dict(job)
    schema = migrated.get("schema_version")
    if schema == 1:
        maximum = migrated.get("max_cycles")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("malformed v1 max_cycles")
        migrated["max_cycles"] = None if maximum == 5 else maximum
        schema = 2
    if schema == 2:
        thread_id = migrated.get("thread_id")
        migrated.update({
            "schema_version": 3,
            "task_id": thread_id,
            "thread_source": "legacy",
            "parent_thread_id": None,
            "parent_task_id": None,
            "root_thread_id": thread_id,
            "agent_path": None,
            "rollout_path": None,
            "goal_source": "legacy",
            "fork_timestamp": None,
            "association_source": "legacy",
            "superseded_by": None,
        })
    if migrated.get("schema_version") == 3:
        # Early 1.3.0 builds used v3 before lineage association provenance was
        # persisted.  Normalize them in place rather than stranding jobs.
        migrated.setdefault("fork_timestamp", None)
        migrated.setdefault("association_source", "legacy")
        migrated.update({
            "schema_version": 4,
            "workspace_kind": "git",
            "workspace_root": migrated.get("project_root"),
            "expected_workspace_snapshot": migrated.get("expected_repo_snapshot"),
        })
    return validate_job(migrated)


def job_state_lock_path(job_path):
    return Path(job_path).with_suffix(".state.lock")


def workspace_mutex_path(codex_home, workspace_root):
    layout = ensure_runtime_layout(codex_home)
    key = hashlib.sha256(str(Path(workspace_root).resolve()).encode("utf-8")).hexdigest()[:24]
    # Keep the v1.4 filename so an in-flight upgraded Git job cannot acquire a
    # second lease under a renamed lock.
    return layout["state"] / f"project-{key}.lock"


def workspace_lease_path(codex_home, workspace_root):
    return workspace_mutex_path(codex_home, workspace_root).with_suffix(".resume.json")


# Public compatibility aliases for v1.4 callers.
project_mutex_path = workspace_mutex_path
project_lease_path = workspace_lease_path


def merge_status(current, requested):
    return current if current in TERMINAL_STATES else requested


@contextmanager
def job_state_locks(job_paths, timeout=10):
    paths = sorted({Path(path).resolve() for path in job_paths}, key=lambda path: path.stem)
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(FileLock(job_state_lock_path(path), timeout=timeout))
        yield paths


def load_job(path, state_locked=False):
    path = Path(path)
    raw = load_json(path)
    migrated = migrate_job(raw)
    if migrated != raw:
        if state_locked:
            atomic_write_json(path, migrated)
        else:
            with FileLock(job_state_lock_path(path), timeout=10):
                current = load_json(path)
                migrated = migrate_job(current)
                if migrated != current:
                    atomic_write_json(path, migrated)
    return migrated


def update_job(path, mutator, timeout=10):
    path = Path(path).resolve()
    with FileLock(job_state_lock_path(path), timeout=timeout):
        job = load_job(path, state_locked=True)
        prior_status = job.get("status")
        mutator(job)
        if prior_status in TERMINAL_STATES:
            job["status"] = prior_status
        save_job(path, job)
        return dict(job)


def save_job(path, job, migrate=True):
    job["updated_at"] = utc_now()
    if migrate:
        migrated = migrate_job(job)
        job.clear()
        job.update(migrated)
    else:
        legacy_required = {
            "schema_version", "job_id", "thread_id", "project_root", "original_goal",
            "status", "billing_policy", "limit_id", "max_cycles", "completed_cycles",
            "poll_interval_seconds", "safety_margin_seconds", "checkpoint_path",
            "expected_repo_snapshot", "watchdog_pid", "created_at", "updated_at", "last_error",
        }
        missing = legacy_required - set(job)
        if missing or job.get("schema_version") not in {1, 2}:
            raise ValueError("invalid raw legacy job")
    atomic_write_json(path, job)


class OwnerState(Enum):
    ABSENT = "absent"
    LIVE_MATCH = "live_match"
    UNKNOWN_OR_IDENTITY_MISMATCH = "unknown_or_identity_mismatch"


class RecoveryState(Enum):
    RETRY_NOW = "retry_now"
    CONTENDED = "contended"
    INACCESSIBLE = "inaccessible"


@dataclass(frozen=True)
class LockSnapshot:
    content: bytes
    identity: tuple


class _GateTimeout(RuntimeError):
    pass


class _AcquisitionGate:
    """Process-scoped serialization for stale-check/delete/recreate transitions."""

    def __init__(self, path, deadline, poll_interval):
        self.path = Path(path).resolve()
        self.deadline = deadline
        self.poll_interval = poll_interval
        self.handle = None
        self.kernel32 = None
        self.fd = None

    def __enter__(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            canonical = os.path.normcase(str(self.path))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            name = f"Local\\CodexAutoResumeFileLock-{digest}"
            handle = kernel32.CreateMutexW(None, False, name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = handle
            self.kernel32 = kernel32
            remaining = max(0.0, self.deadline - time.monotonic())
            milliseconds = min(0xFFFFFFFE, int(remaining * 1000))
            if remaining > 0 and milliseconds == 0:
                milliseconds = 1
            outcome = kernel32.WaitForSingleObject(handle, milliseconds)
            if outcome not in (0x00000000, 0x00000080):
                kernel32.CloseHandle(handle)
                self.handle = None
                self.kernel32 = None
                if outcome == 0x00000102:
                    raise _GateTimeout(f"lock acquisition gate is busy: {self.path}")
                raise ctypes.WinError(ctypes.get_last_error())
            return self

        import fcntl

        gate_path = self.path.with_name(f".{self.path.name}.acquire")
        self.fd = os.open(gate_path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= self.deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise _GateTimeout(f"lock acquisition gate is busy: {self.path}")
                time.sleep(self.poll_interval)
            except BaseException:
                os.close(self.fd)
                self.fd = None
                raise

    def __exit__(self, *_):
        if os.name == "nt":
            import ctypes

            kernel32 = self.kernel32
            try:
                if self.handle and not kernel32.ReleaseMutex(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                if self.handle:
                    kernel32.CloseHandle(self.handle)
                    self.handle = None
                    self.kernel32 = None
            return

        import fcntl

        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


class FileLock:
    def __init__(self, path, timeout=0, poll_interval=0.05):
        self.path = Path(path)
        self.fd = None
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._owner_snapshot = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                with _AcquisitionGate(self.path, deadline, self.poll_interval):
                    outcome, original = self._acquire_under_gate(deadline)
            except _GateTimeout as exc:
                raise RuntimeError(f"job is already locked: {self.path}") from exc
            if outcome is RecoveryState.CONTENDED:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"job is already locked: {self.path}") from original
                time.sleep(self.poll_interval)
                continue
            if outcome is RecoveryState.INACCESSIBLE:
                raise original
            return self

    def _create_lock_file(self):
        return os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    def _acquire_under_gate(self, deadline):
        permission_retries = 0
        while True:
            try:
                self.fd = self._create_lock_file()
            except (FileExistsError, PermissionError) as exc:
                if isinstance(exc, PermissionError):
                    probe_state, _, probe_error = self._read_snapshot()
                    if probe_state is RecoveryState.RETRY_NOW:
                        recovery = RecoveryState.RETRY_NOW
                        recovery_error = exc
                    elif probe_state is RecoveryState.INACCESSIBLE:
                        return RecoveryState.INACCESSIBLE, probe_error or exc
                    else:
                        recovery, recovery_error = self._recover_proven_stale(deadline)
                else:
                    recovery, recovery_error = self._recover_proven_stale(deadline)

                if recovery is RecoveryState.RETRY_NOW:
                    # FileExists plus disappearance always retries under the gate,
                    # even at timeout=0. Repeated PermissionError with no file is
                    # creation/ACL uncertainty and fails closed at the deadline.
                    if isinstance(exc, PermissionError):
                        permission_retries += 1
                        if permission_retries > 1 and time.monotonic() >= deadline:
                            return RecoveryState.INACCESSIBLE, exc
                    continue
                return recovery, recovery_error or exc

            created_identity = self._stat_identity(os.fstat(self.fd))
            owner = {
                "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
                "nonce": uuid.uuid4().hex,
                "created_at": time.time(),
            }
            owner_bytes = json.dumps(owner).encode("utf-8")
            try:
                os.write(self.fd, owner_bytes)
                os.fsync(self.fd)
                stat = os.fstat(self.fd)
                self._owner_snapshot = LockSnapshot(owner_bytes, self._stat_identity(stat))
            except BaseException:
                os.close(self.fd)
                self.fd = None
                self._remove_owned_after_failed_write(created_identity)
                raise
            return None, None

    @staticmethod
    def _stat_identity(stat):
        # st_ctime can change after the writer handle closes on Windows; the
        # volume/file index pair is the stable file-object identity.
        return (stat.st_dev, stat.st_ino)

    def _read_snapshot(self):
        try:
            with self.path.open("rb") as handle:
                content = handle.read()
                handle_identity = self._stat_identity(os.fstat(handle.fileno()))
            path_identity = self._stat_identity(self.path.stat())
        except FileNotFoundError as exc:
            return RecoveryState.RETRY_NOW, None, exc
        except OSError as exc:
            return RecoveryState.INACCESSIBLE, None, exc
        if handle_identity != path_identity:
            return RecoveryState.CONTENDED, None, None
        return None, LockSnapshot(content, handle_identity), None

    def _owner_state(self, snapshot):
        before = snapshot.content
        try:
            owner = json.loads(before.decode("utf-8"))
            pid = owner.get("pid")
            identity = owner.get("process_identity")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH
            if not isinstance(identity, str) or not identity:
                return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH
            try:
                state = process_owner_state(pid, identity)
            except BaseException:
                return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH
        except (ValueError, UnicodeDecodeError, AttributeError):
            try:
                pid = int(before.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH
            try:
                state = process_owner_state(pid, None)
            except BaseException:
                return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH
        try:
            return OwnerState(state)
        except (ValueError, TypeError):
            return OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH

    def _recover_proven_stale(self, deadline):
        first_state, before, first_error = self._read_snapshot()
        if first_state is not None:
            return first_state, first_error
        owner_state = self._owner_state(before)
        if owner_state is OwnerState.LIVE_MATCH:
            return RecoveryState.CONTENDED, None
        if owner_state is OwnerState.UNKNOWN_OR_IDENTITY_MISMATCH:
            return RecoveryState.CONTENDED, None

        # Two fresh content+identity checks make replacement before unlink
        # visible. Cooperating FileLock participants cannot enter this section
        # concurrently because the acquisition gate is held.
        comparisons = 0
        while True:
            compare_state, current, compare_error = self._read_snapshot()
            if compare_state is not None:
                return compare_state, compare_error
            if current != before:
                return RecoveryState.CONTENDED, None
            comparisons += 1
            if comparisons < 2:
                continue
            try:
                self.path.unlink()
                return RecoveryState.RETRY_NOW, None
            except FileNotFoundError as exc:
                return RecoveryState.RETRY_NOW, exc
            except PermissionError as exc:
                # An exited Windows process can briefly retain a sharing lock.
                # Recheck the exact file object before every retry; ACL failures
                # remain inaccessible when the caller's deadline expires.
                if time.monotonic() >= deadline:
                    return RecoveryState.INACCESSIBLE, exc
                time.sleep(self.poll_interval)
            except OSError as exc:
                return RecoveryState.INACCESSIBLE, exc

    def _remove_owned_after_failed_write(self, created_identity):
        try:
            state, snapshot, _ = self._read_snapshot()
            if state is None and snapshot.identity == created_identity:
                self.path.unlink()
        except OSError:
            pass

    def _unlink_with_retry(self, expected_snapshot=None):
        deadline = time.monotonic() + max(1.0, self.poll_interval * 4)
        if expected_snapshot is None:
            while True:
                try:
                    self.path.unlink()
                    return
                except FileNotFoundError:
                    return
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(self.poll_interval)
        while True:
            state, current, error = self._read_snapshot()
            if state is RecoveryState.RETRY_NOW:
                return
            if state is RecoveryState.INACCESSIBLE:
                if time.monotonic() >= deadline:
                    raise error
                time.sleep(self.poll_interval)
                continue
            if expected_snapshot is not None and current != expected_snapshot:
                return
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.poll_interval)

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        deadline = time.monotonic() + max(1.0, self.timeout, self.poll_interval * 4)
        with _AcquisitionGate(self.path, deadline, self.poll_interval):
            self._unlink_with_retry(self._owner_snapshot)
        self._owner_snapshot = None
