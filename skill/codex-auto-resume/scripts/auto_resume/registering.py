import hashlib
import ctypes
import os
import subprocess
import sys
from pathlib import Path

from .checkpoints import write_checkpoint
from .repo import fingerprint, validate_repo
from .resume import validate_thread_id
from .state import ACTIVE_STATES, FileLock, load_job, runtime_home, save_job, utc_now


def windows_creation_flags():
    if os.name != "nt":
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


def _job_id(thread_id, project):
    return hashlib.sha256(f"{thread_id}\0{project}".encode("utf-8")).hexdigest()[:24]


def launch_watchdog(job_path):
    job_path = Path(job_path).resolve()
    job = load_job(job_path)
    watchdog = Path(__file__).parents[1] / "watchdog.py"
    process = subprocess.Popen(
        [sys.executable, str(watchdog), "run", "--job", str(job_path)],
        cwd=job["project_root"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=windows_creation_flags(),
        shell=False,
    )
    job["watchdog_pid"] = process.pid
    save_job(job_path, job)
    return process.pid


start_watchdog = launch_watchdog


def process_is_running(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _register_job(thread_id, project, original_goal, codex_home=None, max_cycles=None,
                 poll_interval_seconds=60, safety_margin_seconds=30, start_watchdog=True):
    thread_id = validate_thread_id(thread_id)
    project = validate_repo(project)
    if not original_goal.strip():
        raise ValueError("original goal is required")
    if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles <= 0):
        raise ValueError("max_cycles must be a positive integer when provided")
    if poll_interval_seconds < 1 or safety_margin_seconds < 0:
        raise ValueError("invalid watchdog timing or cycle settings")
    home = runtime_home(codex_home)
    jobs, checkpoints = home / "jobs", home / "checkpoints"
    jobs.mkdir(parents=True, exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    job_id = _job_id(thread_id, project)
    job_path = jobs / f"{job_id}.json"
    checkpoint_path = checkpoints / f"{job_id}.md"
    with FileLock(jobs / f"{job_id}.register.lock", timeout=10):
        if job_path.exists():
            existing = load_job(job_path)
            same = (existing.get("thread_id") == thread_id and
                    Path(existing.get("project_root", "")) == project)
            if not same:
                raise ValueError(f"job id collision: {job_id}")
            if start_watchdog and existing.get("status") in ACTIVE_STATES and not process_is_running(existing.get("watchdog_pid")):
                launch_watchdog(job_path)
                existing = load_job(job_path)
            return existing, "REUSED"
        created = utc_now()
        job = {
            "schema_version": 2,
            "job_id": job_id,
            "thread_id": thread_id,
            "project_root": str(project),
            "original_goal": original_goal,
            "status": "REGISTERED",
            "billing_policy": "included_only",
            "limit_id": "codex",
            "max_cycles": max_cycles,
            "completed_cycles": 0,
            "poll_interval_seconds": int(poll_interval_seconds),
            "safety_margin_seconds": int(safety_margin_seconds),
            "checkpoint_path": str(checkpoint_path),
            "expected_repo_snapshot": fingerprint(project),
            "watchdog_pid": None,
            "created_at": created,
            "updated_at": created,
            "last_error": None,
        }
        write_checkpoint(checkpoint_path, {
            "THREAD_ID": thread_id,
            "PROJECT": str(project),
            "ORIGINAL_GOAL": original_goal,
            "CURRENT_STATE": "任务已注册；等待 Codex 用量窗口中断。",
            "NEXT_ACTION": "继续当前任务；在每个关键里程碑更新此检查点。",
            "AUTO_RESUME_STATUS": "RUNNING",
        })
        save_job(job_path, job)
        if start_watchdog:
            launch_watchdog(job_path)
            job = load_job(job_path)
        return job, "REGISTERED"


def register_job(*args, **kwargs):
    return _register_job(*args, **kwargs)[0]
