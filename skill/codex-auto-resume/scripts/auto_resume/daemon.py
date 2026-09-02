import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .processes import process_identity, process_is_running
from .activation import ROOT_FALLBACK_GOAL, task_is_ignored
from .registering import (WatchdogStartError, _detach_popen, _register_job,
                          detached_process_options, ensure_watchdog_started)
from .resume import _terminate_process_tree
from .repo import fingerprint
from .session_tasks import (SUBAGENT_FALLBACK_GOAL, discover_session_updates,
                            task_intervals_for_thread)
from .state import (ACTIVE_STATES, FileLock, atomic_write_json, ensure_runtime_layout,
                    load_job, load_json, runtime_home, update_job)
from .watch import publish_child_terminal
from .workspace import resolve_workspace, workspace_from_job


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


def launch_daemon(codex_home=None, popen=None):
    """Launch the supervisor without inheriting a terminal or stdio handles."""
    daemon = Path(__file__).parents[1] / "daemon.py"
    argv = [sys.executable, str(daemon), "run"]
    if codex_home is not None:
        argv.extend(("--codex-home", str(Path(codex_home).expanduser().resolve())))
    return (popen or subprocess.Popen)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, shell=False,
        **detached_process_options(),
    )


def ensure_daemon_started(codex_home=None, handshake_timeout=10, lock_timeout=15,
                          popen=None, sleep=time.sleep):
    """Serialize the daemon check-launch-handshake transition across preflights."""
    layout = ensure_runtime_layout(codex_home)
    with FileLock(layout["state"] / "daemon.startup.lock", timeout=lock_timeout):
        status = daemon_status(codex_home)
        if status.get("running"):
            return status, False
        process = launch_daemon(codex_home, popen=popen)
        deadline = time.monotonic() + handshake_timeout
        while time.monotonic() < deadline:
            status = daemon_status(codex_home, stale_after=max(30, handshake_timeout))
            if status.get("running") and status.get("pid") == process.pid:
                _detach_popen(process)
                return status, True
            if process.poll() is not None:
                if status.get("running"):
                    _detach_popen(process)
                    return status, False
                break
            sleep(0.05)
        if process.poll() is None:
            _terminate_process_tree(process)
        raise RuntimeError("daemon exited before completing its startup handshake")


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


def _existing_jobs(jobs):
    result = []
    for path in jobs.glob("*.json"):
        try:
            result.append((path, load_job(path)))
        except (OSError, ValueError):
            continue
    return result


def _workspace_for_task(task, codex_home):
    return resolve_workspace(
        task["thread_id"], actual_cwd=None, rollout_cwd=task.get("cwd"),
        codex_home=codex_home, parent_thread_id=task.get("parent_thread_id"),
        parent_task_id=task.get("parent_task_id"))


def _parent_for_task(task, existing, sessions_root):
    parents = [job for _, job in existing
               if job.get("thread_id") == task.get("parent_thread_id")]
    forked_at = task.get("fork_timestamp")
    if forked_at is not None:
        try:
            intervals = task_intervals_for_thread(task["parent_thread_id"], sessions_root)
        except (OSError, ValueError):
            intervals = []
        task_ids = {item["task_id"] for item in intervals
                    if item.get("started_at") is not None and item["started_at"] <= forked_at and
                    (item.get("completed_at") is None or forked_at < item["completed_at"])}
        matched = [job for job in parents if job.get("task_id") in task_ids]
        if len(matched) == 1:
            return matched[0], "fork_timestamp"
    explicit = task.get("parent_task_id")
    if explicit:
        matched = [job for job in parents if job.get("task_id") == explicit]
        if len(matched) == 1:
            return matched[0], "rollout_explicit"
    active = [job for job in parents if job.get("status") in ACTIVE_STATES]
    return (active[0], "heuristic_active") if len(active) == 1 else (None, "unassociated")


def _reconcile_authoritative_associations(codex_home, sessions_root, jobs, counts):
    """Let fork-time evidence repair earlier heuristic child associations."""
    existing = _existing_jobs(jobs)
    for _, child in existing:
        if not child.get("parent_thread_id") or child.get("fork_timestamp") is None:
            continue
        parent, source = _parent_for_task(child, existing, sessions_root)
        if parent is None or source != "fork_timestamp":
            continue
        if (child.get("parent_task_id") == parent.get("task_id") and
                child.get("association_source") == "fork_timestamp"):
            continue
        if child.get("association_source") not in {None, "legacy", "unassociated", "heuristic_active",
                                                   "fork_timestamp"}:
            continue
        try:
            _register_job(
                child["thread_id"], workspace_from_job(child), child["original_goal"], codex_home,
                start_watchdog=False, task_id=child["task_id"],
                thread_source=child.get("thread_source", "rollout"),
                parent_thread_id=child["parent_thread_id"], parent_task_id=parent["task_id"],
                root_thread_id=parent.get("root_thread_id") or parent["thread_id"],
                agent_path=child.get("agent_path"), rollout_path=child.get("rollout_path"),
                goal_source=child.get("goal_source", "rollout"),
                fork_timestamp=child.get("fork_timestamp"), association_source="fork_timestamp",
            )
            counts["reconciled"] += 1
        except (OSError, ValueError, RuntimeError) as exc:
            counts["discovery_errors"].append({"rollout": child.get("rollout_path"), "error": str(exc)})


def _discover_and_reconcile(codex_home, sessions_root, jobs):
    result = discover_session_updates(codex_home, sessions_root)
    counts = {"discovered": len(result["tasks"]) + len(result["completed"]),
              "registered": 0, "reconciled": 0, "ignored": 0,
              "deferred": result.get("deferred", 0), "discovery_errors": result["errors"]}
    existing = _existing_jobs(jobs)
    for task in result["completed"]:
        matched = [(path, job) for path, job in existing
                   if job.get("thread_id") == task["thread_id"] and job.get("task_id") == task["task_id"]]
        for path, job in matched:
            if job["status"] in ACTIVE_STATES:
                if job.get("parent_thread_id") and job.get("parent_task_id"):
                    job = publish_child_terminal(
                        path, codex_home, "DONE", final_text=task.get("last_agent_message"),
                        snapshot=fingerprint(workspace_from_job(job)))
                else:
                    job = update_job(path, lambda value: value.update(status="DONE")
                                     if value.get("status") in ACTIVE_STATES else None)
                counts["reconciled"] += 1
    # Roots first: a daemon that was stopped can observe a new parent turn and
    # its child in the same scan.  Register the new parent before associating.
    ordered_tasks = sorted(result["tasks"], key=lambda item: bool(item.get("parent_thread_id")))
    for task in ordered_tasks:
        if task_is_ignored(codex_home, task["thread_id"], task["task_id"]):
            counts["ignored"] += 1
            continue
        try:
            workspace = _workspace_for_task(task, codex_home)
        except (OSError, ValueError, RuntimeError) as exc:
            counts["discovery_errors"].append({"rollout": task.get("rollout_path"), "error": str(exc)})
            continue
        goal = str(task.get("goal") or (SUBAGENT_FALLBACK_GOAL if task.get("parent_thread_id")
                                         else ROOT_FALLBACK_GOAL)).strip()
        goal_source = (task.get("goal_source", "rollout") if task.get("goal") else
                       "subagent_fallback" if task.get("parent_thread_id") else "root_fallback")
        parent_task_id = task.get("parent_task_id")
        parent_job = None
        association_source = ("rollout_explicit" if parent_task_id is not None else "unassociated")
        if task.get("parent_thread_id"):
            parent_job, association_source = _parent_for_task(task, existing, sessions_root)
            if parent_job is not None:
                parent_task_id = parent_job["task_id"]
        try:
            _, outcome = _register_job(
                task["thread_id"], workspace, goal, codex_home, start_watchdog=False,
                task_id=task["task_id"], thread_source=task.get("thread_source", "rollout"),
                parent_thread_id=task.get("parent_thread_id"), parent_task_id=parent_task_id,
                root_thread_id=(parent_job or {}).get("root_thread_id") or task.get("root_thread_id") or task["thread_id"],
                agent_path=task.get("agent_path"), rollout_path=task.get("rollout_path"),
                goal_source=goal_source, interrupted=task.get("interrupted", False),
                fork_timestamp=task.get("fork_timestamp"), association_source=association_source,
            )
            counts["registered"] += outcome == "REGISTERED"
            counts["reconciled"] += outcome == "REUSED"
            existing = _existing_jobs(jobs)
        except (OSError, ValueError, RuntimeError) as exc:
            counts["discovery_errors"].append({"rollout": task.get("rollout_path"), "error": str(exc)})
    _reconcile_authoritative_associations(codex_home, sessions_root, jobs, counts)
    return counts


def scan_once(codex_home=None, sessions_root=None):
    home = runtime_home(codex_home)
    jobs = home / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    discovery = _discover_and_reconcile(codex_home, sessions_root, jobs)
    result = {"examined": 0, "started": 0, "live": 0, "skipped": 0, "errors": [], **discovery}
    for job_path in sorted(path for path in jobs.glob("*.json")
                           if not path.name.endswith(".watchdog.json")):
        result["examined"] += 1
        try:
            job = load_job(job_path)
            if job["status"] not in ACTIVE_STATES:
                result["skipped"] += 1
                continue
            job, started = ensure_watchdog_started(job_path)
            if started:
                result["started"] += 1
            else:
                result["live"] += 1
        except (OSError, ValueError, RuntimeError, WatchdogStartError) as exc:
            result["errors"].append({"job": job_path.name, "error": str(exc)})
    return result


def run_daemon(codex_home=None, interval=10, once=False, sleep=time.sleep, sessions_root=None):
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
            result = scan_once(codex_home, sessions_root=sessions_root)
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
