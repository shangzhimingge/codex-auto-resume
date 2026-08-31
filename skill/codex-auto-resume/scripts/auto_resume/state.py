import json
import os
import hashlib
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .processes import process_identity, process_is_running

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
    "superseded_by", "project_root", "original_goal",
    "status", "billing_policy", "limit_id", "max_cycles", "completed_cycles",
    "poll_interval_seconds", "safety_margin_seconds", "checkpoint_path",
    "expected_repo_snapshot", "watchdog_pid", "created_at", "updated_at", "last_error",
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
    }
    for name in ("jobs", "checkpoints", "logs", "state", "handoffs"):
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
    if job.get("schema_version") != 3:
        raise ValueError("unsupported job schema")
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
    return validate_job(migrated)


def job_state_lock_path(job_path):
    return Path(job_path).with_suffix(".state.lock")


def project_mutex_path(codex_home, project_root):
    layout = ensure_runtime_layout(codex_home)
    key = hashlib.sha256(str(Path(project_root).resolve()).encode("utf-8")).hexdigest()[:24]
    return layout["state"] / f"project-{key}.lock"


def project_lease_path(codex_home, project_root):
    return project_mutex_path(codex_home, project_root).with_suffix(".resume.json")


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


class FileLock:
    def __init__(self, path, timeout=0, poll_interval=0.05):
        self.path = Path(path)
        self.fd = None
        self.timeout = timeout
        self.poll_interval = poll_interval

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if self._recover_proven_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"job is already locked: {self.path}") from exc
                time.sleep(self.poll_interval)
        owner = {
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
            "nonce": uuid.uuid4().hex,
            "created_at": time.time(),
        }
        try:
            os.write(self.fd, json.dumps(owner).encode("utf-8"))
        except BaseException:
            os.close(self.fd)
            self.fd = None
            self._unlink_with_retry()
            raise
        return self

    def _unlink_with_retry(self):
        deadline = time.monotonic() + max(1.0, self.poll_interval * 4)
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

    def _recover_proven_stale(self):
        try:
            before = self.path.read_bytes()
        except OSError:
            return False
        try:
            owner = json.loads(before.decode("utf-8"))
            pid = owner.get("pid")
            identity = owner.get("process_identity")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return False
            if identity is not None and not isinstance(identity, str):
                return False
            stale = not process_is_running(pid, identity)
        except (ValueError, UnicodeDecodeError, AttributeError):
            try:
                pid = int(before.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                return False
            stale = not process_is_running(pid)
        if not stale:
            return False
        try:
            if self.path.read_bytes() != before:
                return False
            self.path.unlink()
            return True
        except OSError:
            return False

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self._unlink_with_retry()
