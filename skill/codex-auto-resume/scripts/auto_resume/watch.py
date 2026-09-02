import hashlib
import os
import time
import uuid
from pathlib import Path

from .checkpoints import read_checkpoint
from .handoffs import (consume_handoffs, finalize_handoff, pending_handoffs,
                       stage_handoff, write_handoff)
from .limits import LimitsError, read_limits, reset_deadline
from .repo import fingerprint, repo_matches
from .processes import process_identity, process_is_running
from .resume import ResumeError, ResumeInterrupted, resume_thread
from .session_tasks import close_resume_launch, record_resume_launch
from .state import (ACTIVE_STATES, TERMINAL_STATES, FileLock, atomic_write_json,
                    ensure_runtime_layout, job_state_lock_path, load_job, load_json,
                    workspace_lease_path, workspace_mutex_path, save_job, update_job)
from .watchdog_lease import WatchdogLease
from .workspace import workspace_from_job

RESUME_PROMPT = """[CODEX_AUTO_RESUME]
读取自动续作检查点：
{checkpoint}
继续完成原始目标：
{goal}
先读取并合并以下未消费的子代理交付：
{handoffs}
核对工作区状态、差异、测试和检查点，只从 NEXT_ACTION 开始。完成全部目标并通过最终验证后，将 AUTO_RESUME_STATUS 写为 DONE。
"""


def decide_action(job, repo_ok, exhausted):
    if job["status"] == "DONE":
        return "done"
    if job["max_cycles"] is not None and job["completed_cycles"] >= job["max_cycles"]:
        return "max_cycles"
    if not repo_ok:
        return "needs_user"
    return "wait" if exhausted else "resume"


def _set(job_path, job, status, error=None, **fields):
    if "expected_workspace_snapshot" in fields:
        fields["expected_repo_snapshot"] = fields["expected_workspace_snapshot"]
    elif "expected_repo_snapshot" in fields:
        fields["expected_workspace_snapshot"] = fields["expected_repo_snapshot"]
    def mutate(current):
        if current.get("status") in TERMINAL_STATES:
            return
        current["status"] = status
        current["last_error"] = error
        current.update(fields)
    fresh = update_job(job_path, mutate)
    job.clear(); job.update(fresh)
    return fresh


def _checkpoint_done(job):
    try:
        return read_checkpoint(job["checkpoint_path"])["AUTO_RESUME_STATUS"].strip().upper() == "DONE"
    except OSError:
        return False


def _settled(project, delay=2):
    first = fingerprint(project)
    time.sleep(delay)
    second = fingerprint(project)
    return second if first == second else None


def _snapshot(job):
    return fingerprint(workspace_from_job(job))


def _snapshot_fields(snapshot):
    return {"expected_workspace_snapshot": snapshot, "expected_repo_snapshot": snapshot}


def _next_wait(deadline, now, poll_interval, safety_margin):
    if deadline <= now:
        return min(float(poll_interval), 1.0)
    return min(float(poll_interval), max(1.0, deadline + safety_margin - now))


def _usage_guard(codex_command, now):
    try:
        snapshot = read_limits(codex_command)
        return "limit_exhausted" if reset_deadline(snapshot, now()) is not None else None
    except LimitsError as exc:
        return f"limits_error:{exc}"


def active_descendants(job, jobs_dir):
    candidates = []
    for path in Path(jobs_dir).glob("*.json"):
        try:
            candidate = load_job(path)
        except (OSError, ValueError):
            continue
        if (candidate.get("root_thread_id") == job.get("root_thread_id") and
                candidate.get("job_id") != job.get("job_id")):
            candidates.append(candidate)
    ancestors = {str(job.get("task_id"))}
    matched = []
    changed = True
    while changed:
        changed = False
        for candidate in candidates:
            if candidate in matched:
                continue
            if str(candidate.get("parent_task_id")) in ancestors:
                matched.append(candidate)
                ancestors.add(str(candidate.get("task_id")))
                changed = True
    return [candidate for candidate in matched if candidate.get("status") in ACTIVE_STATES]


def _project_lock(job, codex_home):
    return workspace_mutex_path(codex_home, job["workspace_root"])


def _project_lease(job, codex_home):
    return workspace_lease_path(codex_home, job["workspace_root"])


def _claim_project(job, codex_home):
    """Claim a durable project lease without holding its mutex during Codex."""
    lock, path, token = _project_lock(job, codex_home), _project_lease(job, codex_home), uuid.uuid4().hex
    with FileLock(lock, timeout=max(10, job["poll_interval_seconds"])):
        try:
            current = load_json(path)
        except (OSError, ValueError):
            current = None
        if isinstance(current, dict) and current.get("owner_token"):
            if process_is_running(current.get("pid"), current.get("process_identity")):
                return None
        atomic_write_json(path, {"owner_token": token, "job_id": job["job_id"],
                                 "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
                                 "claimed_at": time.time(), "descendant_pending": []})
    return token


def _project_preempted(job, codex_home, token, locked=False):
    def inspect():
        try:
            lease = load_json(_project_lease(job, codex_home))
        except (OSError, ValueError):
            return True
        return (lease.get("owner_token") != token or lease.get("job_id") != job["job_id"] or
                bool(lease.get("descendant_pending")))

    if locked:
        return inspect()
    with FileLock(_project_lock(job, codex_home), timeout=max(10, job["poll_interval_seconds"])):
        return inspect()


def _release_project(job, codex_home, token):
    lock, path = _project_lock(job, codex_home), _project_lease(job, codex_home)
    with FileLock(lock, timeout=max(10, job["poll_interval_seconds"])):
        try:
            if load_json(path).get("owner_token") == token:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def _lineage_path(job, codex_home):
    layout = ensure_runtime_layout(codex_home)
    key = hashlib.sha256(
        f"{job['root_thread_id']}\0{job['workspace_root']}".encode("utf-8")
    ).hexdigest()[:24]
    return layout["state"] / f"lineage-{key}.json"


def _advance_lineage(job, codex_home, snapshot):
    atomic_write_json(_lineage_path(job, codex_home), {
        "root_thread_id": job["root_thread_id"], "workspace_kind": job["workspace_kind"],
        "workspace_root": job["workspace_root"], "project_root": job["project_root"],
        "job_id": job["job_id"], "snapshot": snapshot,
    })


def _lineage_accepts(job, codex_home, snapshot):
    if repo_matches(workspace_from_job(job), job["expected_workspace_snapshot"]):
        return True
    try:
        value = load_json(_lineage_path(job, codex_home))
    except (OSError, ValueError):
        return False
    workspace_matches = (
        value.get("workspace_kind") == job.get("workspace_kind") and
        value.get("workspace_root") == job.get("workspace_root"))
    legacy_git_matches = (
        job.get("workspace_kind") == "git" and
        value.get("workspace_kind") is None and
        value.get("project_root") == job.get("workspace_root"))
    return (value.get("root_thread_id") == job.get("root_thread_id") and
            (workspace_matches or legacy_git_matches) and value.get("snapshot") == snapshot)


def _handoff_payload(job, status, final_text=None, events=None):
    if not job.get("parent_thread_id") or not job.get("parent_task_id"):
        return None
    events = events or []
    artifacts = []
    seen_artifacts = set()
    for event in events[-64:]:
        pending, examined = [event], 0
        while pending and examined < 256:
            value = pending.pop(); examined += 1
            if isinstance(value, dict):
                for key in ("artifact_path", "file_path", "output_file"):
                    artifact = value.get(key)
                    if isinstance(artifact, str) and artifact and artifact not in seen_artifacts:
                        seen_artifacts.add(artifact); artifacts.append(artifact)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    return {
        "parent_thread_id": job["parent_thread_id"], "parent_task_id": job["parent_task_id"],
        "child_thread_id": job["thread_id"], "child_task_id": job["task_id"],
        "agent_path": job.get("agent_path"), "status": status, "final_text": final_text,
        "event_summary": [item.get("type") for item in events[-32:] if isinstance(item, dict)],
        "artifacts": artifacts,
    }


def _write_terminal_handoff(job, codex_home, status, final_text=None, events=None):
    payload = _handoff_payload(job, status, final_text, events)
    return write_handoff(codex_home, payload) if payload else None


def _stage_child_handoff(job, codex_home, status="DONE"):
    payload = _handoff_payload(job, status)
    return stage_handoff(codex_home, payload) if payload else None


def _publish_child_terminal_locked(job_path, codex_home, status, final_text=None,
                                   events=None, snapshot=None, error=None):
    """Publish under an already-held project mutex: job-state -> handoff."""
    with FileLock(job_state_lock_path(job_path), timeout=10):
        job = load_job(job_path, state_locked=True)
        requested = status
        payload = _handoff_payload(job, requested, final_text, events)
        if payload:
            try:
                finalize_handoff(codex_home, payload)
            except ValueError as exc:
                requested = "NEEDS_USER"
                error = str(exc)
                payload["status"] = requested
                finalize_handoff(codex_home, payload)
        if job.get("status") not in TERMINAL_STATES:
            job["status"] = requested
            job["last_error"] = error
        if snapshot is not None:
            job.update(_snapshot_fields(snapshot))
        save_job(job_path, job)
        if snapshot is not None:
            _advance_lineage(job, codex_home, snapshot)
        return job


def publish_child_terminal(job_path, codex_home, status, final_text=None,
                           events=None, snapshot=None, error=None):
    job = load_job(job_path)
    with FileLock(_project_lock(job, codex_home), timeout=max(10, job["poll_interval_seconds"])):
        return _publish_child_terminal_locked(job_path, codex_home, status, final_text,
                                              events, snapshot, error)


def run_job(job_path, codex_command=("codex",), sleep=time.sleep, now=time.time, once=False,
            nonce=None):
    job_path = Path(job_path).resolve()
    with FileLock(job_path.with_suffix(".lock")):
        lease = WatchdogLease(job_path, nonce)
        lease.start()
        job = update_job(job_path, lambda value: value.update(watchdog_pid=lease.pid))
        try:
            return _run_job_loop(job_path, codex_command, sleep, now, once, lease)
        finally:
            lease.stop()
            update_job(job_path, lambda value: value.update(watchdog_pid=None)
                       if value.get("watchdog_pid") == lease.pid else None)


def _run_job_loop(job_path, codex_command, sleep, now, once, lease):
    codex_home = (job_path.parents[2] if job_path.parent.name == "jobs" and
                  job_path.parent.parent.name == "auto-resume" else job_path.parent)
    while True:
        lease.heartbeat()
        job = load_job(job_path)
        if _checkpoint_done(job) and not job.get("parent_thread_id"):
            _set(job_path, job, "DONE")
            return "DONE"
        if (_checkpoint_done(job) and job.get("parent_thread_id") and
                job.get("status") in ACTIVE_STATES and job.get("status") != "WAITING_RESET"):
            _set(job_path, job, "WAITING_RESET")
        if job["status"] in {"DONE", "NEEDS_USER", "MAX_CYCLES", "ERROR", "SUPERSEDED"}:
            return job["status"]
        if job["max_cycles"] is not None and job["completed_cycles"] >= job["max_cycles"]:
            if job.get("parent_thread_id"):
                return publish_child_terminal(job_path, codex_home, "MAX_CYCLES")["status"]
            _set(job_path, job, "MAX_CYCLES")
            return "MAX_CYCLES"
        try:
            limits = read_limits(codex_command)
            job["limit_id"] = limits.limit_id
            update_job(job_path, lambda value: value.update(limit_id=limits.limit_id)
                       if value.get("status") not in TERMINAL_STATES else None)
            deadline = reset_deadline(limits, now())
        except LimitsError as exc:
            if job.get("parent_thread_id"):
                return publish_child_terminal(job_path, codex_home, "ERROR", error=str(exc))["status"]
            _set(job_path, job, "ERROR", str(exc))
            return "ERROR"
        if deadline is not None:
            if job["status"] != "WAITING_RESET":
                job.update(_snapshot_fields(_snapshot(job)))
                _advance_lineage(job, codex_home, job["expected_workspace_snapshot"])
            _set(job_path, job, "WAITING_RESET",
                 expected_workspace_snapshot=job.get("expected_workspace_snapshot"))
            wait = _next_wait(deadline, now(), job["poll_interval_seconds"], job["safety_margin_seconds"])
            if once:
                return "WAITING_RESET"
            lease.heartbeat()
            sleep(wait)
            continue
        if job["status"] != "WAITING_RESET":
            _set(job_path, job, "RUNNING")
            if once:
                return "RUNNING"
            lease.heartbeat()
            sleep(job["poll_interval_seconds"])
            continue
        if active_descendants(job, job_path.parent):
            if once:
                return "WAITING_RESET"
            lease.heartbeat()
            sleep(job["poll_interval_seconds"])
            continue
        workspace = workspace_from_job(job)
        project = workspace.root
        project_token = _claim_project(job, codex_home)
        if project_token is None:
            if once:
                return "WAITING_RESET"
            lease.heartbeat()
            sleep(job["poll_interval_seconds"])
            continue
        try:
            with FileLock(_project_lock(job, codex_home), timeout=max(10, job["poll_interval_seconds"])):
              if _project_preempted(job, codex_home, project_token, locked=True):
                  _set(job_path, job, "WAITING_RESET")
                  return "WAITING_RESET"
              settled = _settled(workspace)
              if settled is None or not _lineage_accepts(job, codex_home, settled):
                  error = "workspace changed outside the managed lineage"
                  if job.get("parent_thread_id"):
                      return _publish_child_terminal_locked(
                          job_path, codex_home, "NEEDS_USER", error=error)["status"]
                  _set(job_path, job, "NEEDS_USER", error)
                  return "NEEDS_USER"
              job.update(_snapshot_fields(settled))
              _set(job_path, job, "RESUMING", expected_workspace_snapshot=settled)
              handoffs = pending_handoffs(codex_home, job["thread_id"], job["task_id"])
              handoff_receipts = [(item["path"], item["revision"]) for item in handoffs]
              prompt = RESUME_PROMPT.format(checkpoint=job["checkpoint_path"], goal=job["original_goal"],
                                            handoffs="\n".join(
                                                f"{path}\nrevision={revision}"
                                                for path, revision in handoff_receipts) or "NONE")
            try:
                launch_id = record_resume_launch(codex_home, job)
                try:
                    if _project_preempted(job, codex_home, project_token):
                        _set(job_path, job, "WAITING_RESET")
                        return "WAITING_RESET"
                    result = resume_thread(
                        codex_command, job["thread_id"], prompt, project,
                        env={"CODEX_AUTO_RESUME_JOB_ID": job["job_id"],
                             "CODEX_AUTO_RESUME_TASK_ID": job["task_id"]},
                        supervisor=lambda: _supervise_resume(
                            job_path, lease, codex_command, now, codex_home, project_token),
                        supervisor_interval=min(job["poll_interval_seconds"], 10),
                    )
                finally:
                    close_resume_launch(codex_home, launch_id)
                consume_handoffs(codex_home, job["thread_id"], job["task_id"], handoff_receipts)
            except ResumeInterrupted as exc:
                job = load_job(job_path)
                if exc.reason == "descendant_pending":
                    with FileLock(_project_lock(job, codex_home),
                                  timeout=max(10, job["poll_interval_seconds"])):
                        if exc.thread_verified:
                            snapshot = _snapshot(job)
                            job = update_job(job_path, lambda value: value.update(
                                completed_cycles=value["completed_cycles"] + 1,
                                **_snapshot_fields(snapshot))
                                if value.get("status") not in TERMINAL_STATES else None)
                            _advance_lineage(job, codex_home, snapshot)
                        _set(job_path, job, "WAITING_RESET")
                    if once:
                        return "WAITING_RESET"
                    continue
                if exc.thread_verified:
                    snapshot = _snapshot(job)
                    job = update_job(job_path, lambda value: value.update(
                        completed_cycles=value["completed_cycles"] + 1,
                        **_snapshot_fields(snapshot)) if value.get("status") not in TERMINAL_STATES else None)
                if exc.reason == "superseded":
                    _set(job_path, job, "SUPERSEDED")
                    return "SUPERSEDED"
                if exc.reason == "limit_exhausted":
                    _set(job_path, job, "WAITING_RESET")
                    if once:
                        return "WAITING_RESET"
                    continue
                if exc.reason.startswith("limits_error:"):
                    error = exc.reason.removeprefix("limits_error:")
                    if job.get("parent_thread_id"):
                        return publish_child_terminal(job_path, codex_home, "ERROR", error=error)["status"]
                    _set(job_path, job, "ERROR", error)
                    return "ERROR"
                if job.get("parent_thread_id"):
                    return publish_child_terminal(job_path, codex_home, "NEEDS_USER", error=exc.reason)["status"]
                _set(job_path, job, "NEEDS_USER", exc.reason)
                return "NEEDS_USER"
            except (ResumeError, OSError) as exc:
                if job.get("parent_thread_id"):
                    return publish_child_terminal(job_path, codex_home, "NEEDS_USER", error=str(exc))["status"]
                _set(job_path, job, "NEEDS_USER", str(exc))
                return "NEEDS_USER"
            with FileLock(_project_lock(job, codex_home), timeout=max(10, job["poll_interval_seconds"])):
                snapshot = _snapshot(job)
                terminal_outcome = None
                if _project_preempted(job, codex_home, project_token, locked=True):
                    job = update_job(job_path, lambda value: value.update(
                        **_snapshot_fields(snapshot))
                        if value.get("status") not in TERMINAL_STATES else None)
                    _set(job_path, job, "WAITING_RESET", expected_workspace_snapshot=snapshot)
                    _advance_lineage(job, codex_home, snapshot)
                    terminal_outcome = "WAITING_RESET"
                else:
                    job = update_job(job_path, lambda value: value.update(
                        completed_cycles=value["completed_cycles"] + 1,
                        **_snapshot_fields(snapshot))
                        if value.get("status") not in TERMINAL_STATES else None)
                if terminal_outcome is None and _checkpoint_done(job):
                    if job.get("parent_thread_id"):
                        job = _publish_child_terminal_locked(
                            job_path, codex_home, "DONE", result.final_text, result.events, snapshot)
                    else:
                        _set(job_path, job, "DONE", expected_workspace_snapshot=snapshot)
                        _advance_lineage(job, codex_home, snapshot)
                    terminal_outcome = job["status"]
                elif (terminal_outcome is None and job["max_cycles"] is not None and
                      job["completed_cycles"] >= job["max_cycles"]):
                    if job.get("parent_thread_id"):
                        job = _publish_child_terminal_locked(
                            job_path, codex_home, "MAX_CYCLES", result.final_text, result.events, snapshot)
                    else:
                        _set(job_path, job, "MAX_CYCLES", expected_workspace_snapshot=snapshot)
                        _advance_lineage(job, codex_home, snapshot)
                    terminal_outcome = job["status"]
                elif terminal_outcome is None:
                    _set(job_path, job, "RUNNING", expected_workspace_snapshot=snapshot)
                    _advance_lineage(job, codex_home, snapshot)
        finally:
            # Covers validation failures and early returns inside the lease.
            _release_project(job, codex_home, project_token)
        if terminal_outcome is not None:
            return terminal_outcome
        if once:
            return "RUNNING"
        lease.heartbeat()
        sleep(job["poll_interval_seconds"])


def _supervise_resume(job_path, lease, codex_command, now, codex_home=None,
                      project_token=None):
    lease.heartbeat()
    job = load_job(job_path)
    if job.get("status") == "SUPERSEDED":
        return "superseded"
    if project_token is not None and _project_preempted(job, codex_home, project_token):
        return "descendant_pending"
    return _usage_guard(codex_command, now)
