import time
from pathlib import Path

from .checkpoints import read_checkpoint
from .limits import LimitsError, read_limits, reset_deadline
from .repo import fingerprint, repo_matches
from .resume import ResumeError, ResumeInterrupted, resume_thread
from .state import FileLock, load_job, save_job
from .watchdog_lease import WatchdogLease

RESUME_PROMPT = """读取自动续作检查点：{checkpoint}
继续完成原始目标：{goal}
先核对 git status、git diff、测试状态和检查点，只从 NEXT_ACTION 开始。
不要重复 COMPLETED 或 DO_NOT_REPEAT 中的工作。每个关键里程碑后更新检查点。
完整目标满足且最终验证通过后，将 AUTO_RESUME_STATUS 写为 DONE。"""


def decide_action(job, repo_ok, exhausted):
    if job["status"] == "DONE":
        return "done"
    if job["max_cycles"] is not None and job["completed_cycles"] >= job["max_cycles"]:
        return "max_cycles"
    if not repo_ok:
        return "needs_user"
    return "wait" if exhausted else "resume"


def _set(job_path, job, status, error=None):
    job["status"] = status
    job["last_error"] = error
    save_job(job_path, job)


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


def run_job(job_path, codex_command=("codex",), sleep=time.sleep, now=time.time, once=False,
            nonce=None):
    job_path = Path(job_path).resolve()
    with FileLock(job_path.with_suffix(".lock")):
        lease = WatchdogLease(job_path, nonce)
        lease.start()
        job = load_job(job_path)
        job["watchdog_pid"] = lease.pid
        save_job(job_path, job)
        try:
            return _run_job_loop(job_path, codex_command, sleep, now, once, lease)
        finally:
            lease.stop()
            job = load_job(job_path)
            if job.get("watchdog_pid") == lease.pid:
                job["watchdog_pid"] = None
                save_job(job_path, job)


def _run_job_loop(job_path, codex_command, sleep, now, once, lease):
    while True:
        lease.heartbeat()
        job = load_job(job_path)
        if _checkpoint_done(job):
            _set(job_path, job, "DONE")
            return "DONE"
        if job["status"] in {"DONE", "NEEDS_USER", "MAX_CYCLES", "ERROR"}:
            return job["status"]
        if job["max_cycles"] is not None and job["completed_cycles"] >= job["max_cycles"]:
            _set(job_path, job, "MAX_CYCLES")
            return "MAX_CYCLES"
        try:
            limits = read_limits(codex_command)
            job["limit_id"] = limits.limit_id
            deadline = reset_deadline(limits, now())
        except LimitsError as exc:
            _set(job_path, job, "ERROR", str(exc))
            return "ERROR"
        if deadline is not None:
            _set(job_path, job, "WAITING_RESET")
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
        project = Path(job["project_root"])
        settled = _settled(project)
        if settled is None or not repo_matches(project, job["expected_repo_snapshot"]):
            _set(job_path, job, "NEEDS_USER", "repository changed since the last checkpoint")
            return "NEEDS_USER"
        _set(job_path, job, "RESUMING")
        prompt = RESUME_PROMPT.format(checkpoint=job["checkpoint_path"], goal=job["original_goal"])
        try:
            resume_thread(
                codex_command, job["thread_id"], prompt, project,
                supervisor=lambda: _supervise_resume(lease, codex_command, now),
                supervisor_interval=min(job["poll_interval_seconds"], 10),
            )
        except ResumeInterrupted as exc:
            job = load_job(job_path)
            if exc.thread_verified:
                job["completed_cycles"] += 1
                job["expected_repo_snapshot"] = fingerprint(project)
            if exc.reason == "limit_exhausted":
                _set(job_path, job, "WAITING_RESET")
                if once:
                    return "WAITING_RESET"
                continue
            if exc.reason.startswith("limits_error:"):
                _set(job_path, job, "ERROR", exc.reason.removeprefix("limits_error:"))
                return "ERROR"
            _set(job_path, job, "NEEDS_USER", exc.reason)
            return "NEEDS_USER"
        except (ResumeError, OSError) as exc:
            _set(job_path, job, "NEEDS_USER", str(exc))
            return "NEEDS_USER"
        job = load_job(job_path)
        job["completed_cycles"] += 1
        job["expected_repo_snapshot"] = fingerprint(project)
        if _checkpoint_done(job):
            _set(job_path, job, "DONE")
            return "DONE"
        if job["max_cycles"] is not None and job["completed_cycles"] >= job["max_cycles"]:
            _set(job_path, job, "MAX_CYCLES")
            return "MAX_CYCLES"
        _set(job_path, job, "RUNNING")
        if once:
            return "RUNNING"
        lease.heartbeat()
        sleep(job["poll_interval_seconds"])

def _supervise_resume(lease, codex_command, now):
    lease.heartbeat()
    return _usage_guard(codex_command, now)
