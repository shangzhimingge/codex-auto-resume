import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .checkpoints import write_checkpoint
from .repo import fingerprint
from .resume import _terminate_process_tree, validate_thread_id
from .state import (ACTIVE_STATES, FileLock, atomic_write_json, job_state_locks,
                    load_job, load_json, workspace_lease_path, workspace_mutex_path,
                    runtime_home, save_job, utc_now)
from .watchdog_lease import read_lease, watchdog_lease_is_live
from .workspace import Workspace, resolve_workspace, workspace_from_job


def windows_creation_flags():
    if os.name != "nt":
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


def detached_process_options():
    if os.name == "nt":
        return {"creationflags": windows_creation_flags()}
    return {"start_new_session": True}


def _job_id(thread_id, task_id, project):
    return hashlib.sha256(f"{thread_id}\0{task_id}\0{project}".encode("utf-8")).hexdigest()[:24]


def _registration_lock_id(thread_id, project):
    return hashlib.sha256(f"{thread_id}\0{project}".encode("utf-8")).hexdigest()[:24]


class WatchdogStartError(RuntimeError):
    pass


def _detach_popen(process):
    if os.name == "nt":
        process._handle.Close()
        process._child_created = False
    else:
        threading.Thread(target=process.wait, daemon=True).start()


def launch_watchdog(job_path, codex_command=None, handshake_timeout=10):
    job_path = Path(job_path).resolve()
    job = load_job(job_path)
    watchdog = Path(__file__).parents[1] / "watchdog.py"
    nonce = uuid.uuid4().hex
    argv = [sys.executable, str(watchdog), "run", "--job", str(job_path), "--nonce", nonce]
    if codex_command is not None:
        argv.extend(("--codex-command-json", json.dumps(list(codex_command))))
    process = subprocess.Popen(
        argv, cwd=job["workspace_root"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, shell=False, **detached_process_options(),
    )
    deadline = time.monotonic() + handshake_timeout
    verified = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        lease = read_lease(job_path)
        if (lease and lease.get("nonce") == nonce and lease.get("pid") == process.pid and
                watchdog_lease_is_live(job_path, process.pid, stale_after=max(5, handshake_timeout)) and
                load_job(job_path).get("watchdog_pid") == process.pid):
            time.sleep(0.1)
            if (process.poll() is None and
                    watchdog_lease_is_live(job_path, process.pid, stale_after=max(5, handshake_timeout))):
                verified = True
                break
        time.sleep(0.05)
    if not verified:
        if process.poll() is None:
            _terminate_process_tree(process)
        raise WatchdogStartError("watchdog exited before completing its startup handshake")
    _detach_popen(process)
    return process.pid


start_watchdog = launch_watchdog


def ensure_watchdog_started(job_path, codex_command=None, handshake_timeout=10,
                            lock_timeout=15):
    """Serialize the check-and-launch transition for one durable job."""
    job_path = Path(job_path).resolve()
    startup_lock = job_path.with_suffix(".startup.lock")
    with FileLock(startup_lock, timeout=lock_timeout):
        job = load_job(job_path)
        if job.get("status") not in ACTIVE_STATES:
            return job, False
        stale_after = max(30, job["poll_interval_seconds"] * 3)
        if watchdog_lease_is_live(job_path, job.get("watchdog_pid"), stale_after=stale_after):
            return job, False
        launch_watchdog(job_path, codex_command=codex_command,
                        handshake_timeout=handshake_timeout)
        return load_job(job_path), True

ASSOCIATION_PRIORITY = {"legacy": 0, "unassociated": 0, "heuristic_active": 1,
                        "rollout_explicit": 2, "fork_timestamp": 3, "explicit": 4}


def merge_registration(existing, incoming):
    """Merge lineage metadata while preserving runtime-owned fields."""
    merged = dict(existing)
    for field in ("parent_thread_id", "agent_path", "rollout_path"):
        if merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]
    if merged.get("fork_timestamp") is None and incoming.get("fork_timestamp") is not None:
        merged["fork_timestamp"] = incoming["fork_timestamp"]
    current_priority = ASSOCIATION_PRIORITY.get(merged.get("association_source"), 0)
    incoming_priority = ASSOCIATION_PRIORITY.get(incoming.get("association_source"), 0)
    if incoming.get("parent_task_id") is not None and incoming_priority > current_priority:
        merged["parent_task_id"] = incoming["parent_task_id"]
        merged["association_source"] = incoming["association_source"]
        if incoming.get("root_thread_id") is not None:
            merged["root_thread_id"] = incoming["root_thread_id"]
    elif (merged.get("association_source") in {None, "legacy", "unassociated"} and
          incoming.get("association_source") not in {None, "legacy", "unassociated"}):
        merged["association_source"] = incoming["association_source"]
    if incoming.get("interrupted") and merged.get("status") in ACTIVE_STATES:
        merged["status"] = "WAITING_RESET"
    return merged


def _owner_is_ancestor(owner, child, jobs):
    by_identity = {(item.get("thread_id"), str(item.get("task_id"))): item
                   for item in jobs if item.get("thread_id") and item.get("task_id") is not None}
    parent = (child.get("parent_thread_id"), str(child.get("parent_task_id")))
    visited = set()
    while parent[0] and parent[1] not in {"", "None"} and parent not in visited:
        if (owner.get("thread_id"), str(owner.get("task_id"))) == parent:
            return True
        visited.add(parent)
        candidate = by_identity.get(parent)
        if candidate is None:
            return False
        parent = (candidate.get("parent_thread_id"), str(candidate.get("parent_task_id")))
    return False


def _mark_ancestor_lease_pending(codex_home, project, child, jobs):
    """Mark a live project claimant when a newly visible descendant appears.

    The caller holds the project mutex, so the lease document needs no second
    lock and preserves the global project -> job-state ordering.
    """
    path = workspace_lease_path(codex_home, project)
    try:
        lease = load_json(path)
    except (OSError, ValueError):
        return False
    owner_id = lease.get("job_id") if isinstance(lease, dict) else None
    owner = next((item for item in jobs if item.get("job_id") == owner_id), None)
    if owner is None or owner_id == child.get("job_id") or not _owner_is_ancestor(owner, child, jobs):
        return False
    pending = lease.setdefault("descendant_pending", [])
    if child["job_id"] not in pending:
        pending.append(child["job_id"])
        lease["descendant_pending_at"] = time.time()
        atomic_write_json(path, lease)
    return True


def _register_job(thread_id, project, original_goal, codex_home=None, max_cycles=None,
                  poll_interval_seconds=60, safety_margin_seconds=30, start_watchdog=True,
                  watchdog_codex_command=None, task_id=None, thread_source="explicit",
                  parent_thread_id=None, parent_task_id=None, root_thread_id=None,
                  agent_path=None, rollout_path=None, goal_source="explicit",
                  interrupted=False, fork_timestamp=None, association_source=None,
                  workspace_kind=None):
    thread_id = validate_thread_id(thread_id)
    task_id = str(task_id or thread_id).strip()
    if not task_id:
        raise ValueError("task id is required")
    if parent_thread_id is not None:
        parent_thread_id = validate_thread_id(str(parent_thread_id))
    if fork_timestamp is not None:
        fork_timestamp = float(fork_timestamp)
    association_source = str(association_source or
                             ("explicit" if parent_task_id is not None else "unassociated"))
    root_thread_id = validate_thread_id(str(root_thread_id or thread_id))
    workspace = (project if isinstance(project, Workspace) else
                 (Workspace(workspace_kind, project) if workspace_kind else
                  resolve_workspace(thread_id, explicit=project, codex_home=codex_home)))
    project = workspace.root
    if not original_goal.strip():
        raise ValueError("original goal is required")
    if max_cycles is not None and (isinstance(max_cycles, bool) or
                                   not isinstance(max_cycles, int) or max_cycles <= 0):
        raise ValueError("max_cycles must be a positive integer when provided")
    if poll_interval_seconds < 1 or safety_margin_seconds < 0:
        raise ValueError("invalid watchdog timing or cycle settings")

    home = runtime_home(codex_home)
    jobs, checkpoints = home / "jobs", home / "checkpoints"
    jobs.mkdir(parents=True, exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    job_id = _job_id(thread_id, task_id, project)
    job_path = jobs / f"{job_id}.json"
    checkpoint_path = checkpoints / f"{job_id}.md"
    boundary_lock = jobs / f"{_registration_lock_id(thread_id, project)}.register.lock"
    created = utc_now()
    snapshot = fingerprint(workspace)
    new_job = {
        "schema_version": 4, "job_id": job_id, "thread_id": thread_id, "task_id": task_id,
        "thread_source": str(thread_source), "parent_thread_id": parent_thread_id,
        "parent_task_id": str(parent_task_id) if parent_task_id is not None else None,
        "root_thread_id": root_thread_id,
        "agent_path": str(agent_path) if agent_path is not None else None,
        "rollout_path": str(Path(rollout_path).resolve()) if rollout_path else None,
        "goal_source": str(goal_source), "fork_timestamp": fork_timestamp,
        "association_source": association_source, "superseded_by": None,
        "workspace_kind": workspace.kind, "workspace_root": str(project),
        "project_root": str(project), "original_goal": original_goal,
        "status": "WAITING_RESET" if interrupted else "REGISTERED",
        "billing_policy": "included_only", "limit_id": "codex", "max_cycles": max_cycles,
        "completed_cycles": 0, "poll_interval_seconds": int(poll_interval_seconds),
        "safety_margin_seconds": int(safety_margin_seconds), "checkpoint_path": str(checkpoint_path),
        "expected_workspace_snapshot": snapshot,
        "expected_repo_snapshot": snapshot, "watchdog_pid": None,
        "created_at": created, "updated_at": created, "last_error": None,
    }
    incoming = {
        "parent_thread_id": parent_thread_id,
        "parent_task_id": str(parent_task_id) if parent_task_id is not None else None,
        "root_thread_id": root_thread_id,
        "agent_path": str(agent_path) if agent_path is not None else None,
        "rollout_path": str(Path(rollout_path).resolve()) if rollout_path else None,
        "fork_timestamp": fork_timestamp, "association_source": association_source,
        "interrupted": bool(interrupted),
    }

    with FileLock(boundary_lock, timeout=10):
        with FileLock(workspace_mutex_path(codex_home, project), timeout=10):
            all_paths = sorted(path for path in jobs.glob("*.json")
                               if not path.name.endswith(".watchdog.json"))
            with job_state_locks([*all_paths, job_path], timeout=10):
                loaded_jobs = []
                for candidate_path in all_paths:
                    try:
                        loaded_jobs.append(load_job(candidate_path, state_locked=True))
                    except (OSError, ValueError):
                        continue
                if job_path.exists():
                    existing = next((item for item in loaded_jobs
                                     if item.get("job_id") == job_id),
                                    load_job(job_path, state_locked=True))
                    existing_workspace = workspace_from_job(existing)
                    same = (existing.get("thread_id") == thread_id and
                            existing.get("task_id") == task_id and
                            existing_workspace == workspace)
                    if not same:
                        raise ValueError(f"job id collision: {job_id}")
                    result = merge_registration(existing, incoming)
                    save_job(job_path, result)
                    outcome = "REUSED"
                else:
                    by_id = {item.get("job_id"): item for item in loaded_jobs}
                    for candidate_path in all_paths:
                        candidate = by_id.get(candidate_path.stem)
                        if candidate is None:
                            continue
                        if (candidate.get("thread_id") == thread_id and
                                workspace_from_job(candidate) == workspace and
                                candidate.get("status") in ACTIVE_STATES):
                            candidate["status"] = "SUPERSEDED"
                            candidate["superseded_by"] = job_id
                            save_job(candidate_path, candidate)
                    write_checkpoint(checkpoint_path, {
                        "THREAD_ID": thread_id, "TASK_ID": task_id,
                        "ROOT_THREAD_ID": root_thread_id,
                        "PARENT_THREAD_ID": parent_thread_id or "",
                        "PARENT_TASK_ID": parent_task_id or "", "AGENT_PATH": agent_path or "",
                        "ROLLOUT_PATH": str(Path(rollout_path).resolve()) if rollout_path else "",
                        "PROJECT": str(project), "ORIGINAL_GOAL": original_goal,
                        "CURRENT_STATE": "Task registered; waiting for an included-window interruption.",
                        "NEXT_ACTION": "Continue the task and update this checkpoint at milestones.",
                        "AUTO_RESUME_STATUS": "RUNNING",
                    })
                    save_job(job_path, new_job)
                    result, outcome = new_job, "REGISTERED"
                lineage_jobs = [item for item in loaded_jobs if item.get("job_id") != result["job_id"]]
                lineage_jobs.append(result)
                _mark_ancestor_lease_pending(codex_home, project, result, lineage_jobs)

    if start_watchdog and result.get("status") in ACTIVE_STATES:
        result, _ = ensure_watchdog_started(job_path, codex_command=watchdog_codex_command)
    return result, outcome


def register_job(*args, **kwargs):
    return _register_job(*args, **kwargs)[0]
