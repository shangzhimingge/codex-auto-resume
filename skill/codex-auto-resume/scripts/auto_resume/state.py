import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .processes import process_identity, process_is_running

STATUSES = {
    "REGISTERED", "RUNNING", "WAITING_RESET", "RESUMING", "DONE",
    "NEEDS_USER", "MAX_CYCLES", "ERROR",
}
ACTIVE_STATES = {"REGISTERED", "RUNNING", "WAITING_RESET", "RESUMING"}
REQUIRED_JOB_FIELDS = (
    "schema_version", "job_id", "thread_id", "project_root", "original_goal",
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
    }
    for name in ("jobs", "checkpoints", "logs", "state"):
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
    if job.get("schema_version") != 2:
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
        migrated["schema_version"] = 2
        migrated["max_cycles"] = None if maximum == 5 else maximum
    return validate_job(migrated)


def load_job(path):
    raw = load_json(path)
    migrated = migrate_job(raw)
    if migrated != raw:
        atomic_write_json(path, migrated)
    return migrated


def save_job(path, job, migrate=True):
    job["updated_at"] = utc_now()
    if migrate:
        migrated = migrate_job(job)
        job.clear()
        job.update(migrated)
    else:
        missing = set(REQUIRED_JOB_FIELDS) - set(job)
        if missing or job.get("schema_version") != 1:
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
