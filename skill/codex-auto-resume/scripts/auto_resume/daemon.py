import json
import os
import signal
import time
import uuid
from pathlib import Path

from .processes import process_identity, process_is_running
from .registering import WatchdogStartError, launch_watchdog
from .state import (ACTIVE_STATES, FileLock, atomic_write_json, ensure_runtime_layout,
                    load_job, load_json, runtime_home)
from .watchdog_lease import watchdog_lease_is_live


def daemon_state_path(codex_home=None):
    layout = ensure_runtime_layout(codex_home, best_effort=True)
    preferred = layout["state"] / "daemon-state.json"
    legacy = layout["root"] / "daemon-state.json"
    return preferred if preferred.exists() or not legacy.exists() else legacy


def _daemon_state(codex_home=None):
    try:
        return load_json(daemon_state_path(codex_home))
    except (OSError, ValueError):
        return None


def daemon_status(codex_home=None, stale_after=30):
    state = _daemon_state(codex_home)
    if not isinstance(state, dict):
        return {"running": False}
    pid, identity, heartbeat = state.get("pid"), state.get("process_identity"), state.get("heartbeat_at")
    live = (isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool) and
            -5 <= time.time() - heartbeat <= stale_after and
            process_is_running(pid, identity))
    return {**state, "running": bool(live)}


def stop_daemon(codex_home=None, timeout=10, sleep=time.sleep):
    state = _daemon_state(codex_home)
    if not isinstance(state, dict):
        return {"stopped": False, "reason": "not_running"}
    pid, identity = state.get("pid"), state.get("process_identity")
    if not process_is_running(pid, identity):
        return {"stopped": False, "reason": "not_running"}
    layout = ensure_runtime_layout(codex_home, best_effort=True)
    preferred_lock = layout["state"] / "daemon.lock"
    legacy_lock = layout["root"] / "daemon.lock"
    lock_path = preferred_lock if preferred_lock.exists() or not legacy_lock.exists() else legacy_lock
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_running(pid, identity):
            cleanup_deadline = time.monotonic() + 2
            while time.monotonic() < cleanup_deadline:
                try:
                    before = lock_path.read_bytes()
                    owner = json.loads(before.decode("utf-8"))
                    if (owner.get("pid") != pid or
                            owner.get("process_identity") != identity or
                            lock_path.read_bytes() != before):
                        break
                    lock_path.unlink()
                    break
                except FileNotFoundError:
                    break
                except (OSError, ValueError, UnicodeDecodeError, AttributeError):
                    sleep(0.05)
            return {"stopped": True, "pid": pid}
        sleep(0.05)
    raise RuntimeError(f"daemon did not stop within {timeout} seconds: {pid}")


def scan_once(codex_home=None):
    home = runtime_home(codex_home)
    jobs = home / "jobs"
    result = {"examined": 0, "started": 0, "live": 0, "skipped": 0, "errors": []}
    if not jobs.is_dir():
        return result
    for job_path in sorted(jobs.glob("*.json")):
        result["examined"] += 1
        try:
            job = load_job(job_path)
            if job["status"] not in ACTIVE_STATES:
                result["skipped"] += 1
                continue
            stale_after = max(30, job["poll_interval_seconds"] * 3)
            if watchdog_lease_is_live(job_path, job.get("watchdog_pid"), stale_after):
                result["live"] += 1
                continue
            register_lock = jobs / f"{job['job_id']}.register.lock"
            with FileLock(register_lock, timeout=0.2):
                job = load_job(job_path)
                if watchdog_lease_is_live(job_path, job.get("watchdog_pid"), stale_after):
                    result["live"] += 1
                else:
                    launch_watchdog(job_path)
                    result["started"] += 1
        except (OSError, ValueError, RuntimeError, WatchdogStartError) as exc:
            result["errors"].append({"job": job_path.name, "error": str(exc)})
    return result


def run_daemon(codex_home=None, interval=10, once=False, sleep=time.sleep):
    layout = ensure_runtime_layout(codex_home)
    home = layout["root"]
    stop = {"requested": False}

    def request_stop(*_):
        stop["requested"] = True

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    with FileLock(layout["state"] / "daemon.lock"):
        nonce = uuid.uuid4().hex
        while True:
            result = scan_once(codex_home)
            atomic_write_json(daemon_state_path(codex_home), {
                "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
                "nonce": nonce,
                "heartbeat_at": time.time(),
                "last_scan": result,
            })
            if once or stop["requested"]:
                return result
            sleep(max(1, interval))
