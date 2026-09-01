import hashlib
import os
import time
from pathlib import Path

from .registering import _register_job
from .repo import validate_repo
from .resume import validate_thread_id
from .session_tasks import (SUBAGENT_FALLBACK_GOAL, confirm_resume_launch_for_job,
                            git_root, resolve_current_task)
from .state import FileLock, atomic_write_json, ensure_runtime_layout, load_job, load_json, runtime_home


def _ignored_path(codex_home):
    return runtime_home(codex_home) / "state" / "ignored-tasks.json"


def _ignore_task(codex_home, thread_id, task_id):
    if not thread_id or not task_id:
        return
    try:
        path = ensure_runtime_layout(codex_home)["state"] / "ignored-tasks.json"
        with FileLock(path.with_suffix(".lock"), timeout=5):
            try:
                value = load_json(path)
            except (OSError, ValueError):
                value = {}
            value[hashlib.sha256(f"{thread_id}\0{task_id}".encode()).hexdigest()] = {
                "thread_id": thread_id, "task_id": task_id,
            }
            atomic_write_json(path, value)
    except OSError:
        # Opt-out remains effective for this invocation even if persistence is unavailable.
        return


def task_is_ignored(codex_home, thread_id, task_id):
    try:
        value = load_json(_ignored_path(codex_home))
    except (OSError, ValueError):
        return False
    key = hashlib.sha256(f"{thread_id}\0{task_id}".encode()).hexdigest()
    return key in value


def _record_resume_attempt(codex_home, job, discovered):
    path = ensure_runtime_layout(codex_home)["state"] / "resume-attempts.json"
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            value = {}
        attempts = value.setdefault(job["job_id"], [])
        attempts.append({"at": time.time(), "job_id": job["job_id"],
                         "task_id": job["task_id"],
                         "resume_turn_id": (discovered or {}).get("task_id"),
                         "rollout_path": (discovered or {}).get("rollout_path")})
        value[job["job_id"]] = attempts[-128:]
        atomic_write_json(path, value)


def _ensure_daemon_for_preflight(codex_home, start_watchdog):
    if start_watchdog:
        # Local import avoids daemon -> activation -> daemon recursion while
        # keeping daemon discovery registrations side-effect free.
        from .daemon import ensure_daemon_started
        ensure_daemon_started(codex_home)


def preflight(thread_id=None, project=None, goal=None, codex_home=None, opt_out=False,
              start_watchdog=True, max_cycles=None, task_id=None, parent_thread_id=None,
              parent_task_id=None, root_thread_id=None, agent_path=None, rollout_path=None,
              sessions_root=None, fork_timestamp=None, association_source=None):
    env_thread = os.environ.get("CODEX_THREAD_ID")
    if thread_id and env_thread and str(thread_id) != env_thread:
        return {"outcome": "SKIPPED", "reason": "thread_mismatch"}
    # A resumed Codex invocation starts a fresh turn in the same thread.  Bind it
    # to the durable job before inspecting that new turn, otherwise it can
    # supersede the very job that launched it.
    internal_job_id = os.environ.get("CODEX_AUTO_RESUME_JOB_ID")
    internal_task_id = os.environ.get("CODEX_AUTO_RESUME_TASK_ID")
    if internal_job_id or internal_task_id:
        try:
            existing = load_job(ensure_runtime_layout(codex_home)["jobs"] / f"{internal_job_id}.json")
            effective_thread = str(thread_id or env_thread or "")
            if (not internal_job_id or not internal_task_id or
                    effective_thread != existing["thread_id"] or
                    internal_task_id != existing["task_id"]):
                raise ValueError("internal identity mismatch")
            candidate_project = validate_repo(Path(project)) if project else git_root(Path.cwd())
            if candidate_project is None or Path(existing["project_root"]) != candidate_project:
                raise ValueError("internal project mismatch")
            if opt_out:
                _ignore_task(codex_home, existing["thread_id"], existing["task_id"])
                return {"outcome": "SKIPPED", "reason": "explicit_opt_out"}
            discovered = None
            try:
                discovered = resolve_current_task(effective_thread, sessions_root)
            except (ValueError, OSError):
                pass
            if (discovered and discovered.get("task_id") and
                    discovered.get("thread_id") == effective_thread):
                confirm_resume_launch_for_job(
                    codex_home, existing["job_id"], existing["task_id"],
                    discovered["thread_id"], discovered["task_id"],
                    discovered.get("started_at"))
            _record_resume_attempt(codex_home, existing, discovered)
        except (OSError, ValueError, RuntimeError):
            return {"outcome": "SKIPPED", "reason": "internal_identity_mismatch"}
        _ensure_daemon_for_preflight(codex_home, start_watchdog)
        return {"outcome": "REUSED", "job": existing, "resume_attempt": True}
    effective_thread = thread_id or env_thread
    discovered = None
    if effective_thread:
        try:
            discovered = resolve_current_task(effective_thread, sessions_root)
        except (ValueError, OSError):
            discovered = None
    effective_thread = str(effective_thread or (discovered or {}).get("thread_id") or "")
    effective_task = str(task_id or (discovered or {}).get("task_id") or "")
    if opt_out:
        _ignore_task(codex_home, effective_thread or None, effective_task or None)
        return {"outcome": "SKIPPED", "reason": "explicit_opt_out"}
    if not effective_thread:
        return {"outcome": "SKIPPED", "reason": "missing_thread"}
    if not effective_task:
        # Explicit legacy registration remains a single task per thread.
        effective_task = effective_thread if thread_id and not discovered else ""
    if not effective_task:
        return {"outcome": "SKIPPED", "reason": "missing_task"}
    if task_is_ignored(codex_home, effective_thread, effective_task):
        return {"outcome": "SKIPPED", "reason": "task_opted_out"}
    candidate_project = Path(project) if project else git_root((discovered or {}).get("cwd") or Path.cwd())
    if candidate_project is None:
        return {"outcome": "SKIPPED", "reason": "missing_project"}
    effective_goal = str(goal if goal is not None else (discovered or {}).get("goal") or "").strip()
    if not effective_goal and (discovered or {}).get("parent_thread_id"):
        effective_goal = SUBAGENT_FALLBACK_GOAL
    if not effective_goal:
        return {"outcome": "SKIPPED", "reason": "missing_goal"}
    try:
        effective_thread = validate_thread_id(effective_thread)
        candidate_project = validate_repo(candidate_project)
    except (ValueError, RuntimeError, OSError):
        return {"outcome": "SKIPPED", "reason": "ineligible_context"}
    metadata = discovered or {}
    effective_parent_task = parent_task_id if parent_task_id is not None else metadata.get("parent_task_id")
    effective_association = association_source
    if effective_association is None:
        if parent_task_id is not None:
            effective_association = "explicit"
        elif metadata.get("parent_task_id") is not None:
            effective_association = "rollout_explicit"
        else:
            effective_association = "unassociated"
    job, outcome = _register_job(
        effective_thread, candidate_project, effective_goal, codex_home,
        max_cycles=max_cycles, start_watchdog=start_watchdog, task_id=effective_task,
        thread_source=metadata.get("thread_source", "explicit"),
        parent_thread_id=parent_thread_id or metadata.get("parent_thread_id"),
        parent_task_id=effective_parent_task,
        root_thread_id=root_thread_id or metadata.get("root_thread_id") or effective_thread,
        agent_path=agent_path or metadata.get("agent_path"),
        rollout_path=rollout_path or metadata.get("rollout_path"),
        goal_source="explicit" if goal is not None else metadata.get("goal_source", "rollout"),
        interrupted=bool(metadata.get("interrupted")),
        fork_timestamp=(fork_timestamp if fork_timestamp is not None else metadata.get("fork_timestamp")),
        association_source=effective_association,
    )
    # _register_job releases its registration and job-state locks before it
    # returns. Start the shared supervisor only for a qualified preflight.
    _ensure_daemon_for_preflight(codex_home, start_watchdog)
    return {"outcome": outcome, "job": job}
