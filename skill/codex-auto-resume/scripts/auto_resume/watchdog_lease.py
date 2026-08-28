import os
import time
import uuid
from pathlib import Path

from .processes import process_identity, process_is_running
from .state import atomic_write_json, load_json


def lease_path(job_path):
    return Path(job_path).with_suffix(".watchdog.json")


def read_lease(job_path):
    try:
        value = load_json(lease_path(job_path))
    except (OSError, ValueError):
        return None
    required = {"pid", "process_identity", "nonce", "heartbeat_at", "state"}
    return value if isinstance(value, dict) and required <= set(value) else None


def watchdog_lease_is_live(job_path, pid, stale_after):
    lease = read_lease(job_path)
    if not lease or lease["state"] != "running" or lease["pid"] != pid:
        return False
    if not isinstance(lease["nonce"], str) or not lease["nonce"]:
        return False
    identity = lease["process_identity"]
    if not isinstance(identity, str) or not identity:
        return False
    heartbeat = lease["heartbeat_at"]
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return False
    age = time.time() - heartbeat
    if age < -5 or age > stale_after:
        return False
    return process_is_running(pid, identity)


class WatchdogLease:
    def __init__(self, job_path, nonce=None):
        self.job_path = Path(job_path)
        self.nonce = nonce or uuid.uuid4().hex
        self.pid = os.getpid()
        self.identity = process_identity(self.pid)
        if not self.identity:
            raise RuntimeError("watchdog process identity is unavailable")

    def _write(self, state):
        atomic_write_json(lease_path(self.job_path), {
            "pid": self.pid,
            "process_identity": self.identity,
            "nonce": self.nonce,
            "heartbeat_at": time.time(),
            "state": state,
        })

    def start(self):
        self._write("running")

    def heartbeat(self):
        self._write("running")

    def stop(self):
        self._write("stopped")
