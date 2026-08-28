import hashlib
import json
import os
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path

from .checkpoints import write_checkpoint
from .repo import fingerprint, validate_repo
from .resume import _terminate_process_tree, validate_thread_id
from .state import ACTIVE_STATES, FileLock, load_job, runtime_home, save_job, utc_now
from .watchdog_lease import read_lease, watchdog_lease_is_live


def windows_creation_flags():
    if os.name != "nt":
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


def detached_process_options():
    if os.name == "nt":
        return {"creationflags": windows_creation_flags()}
    return {"start_new_session": True}


def _job_id(thread_id, project):
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
        argv,
        cwd=job["project_root"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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


def _register_job(thread_id, project, original_goal, codex_home=None, max_cycles=None,
                 poll_interval_seconds=60, safety_margin_seconds=30, start_watchdog=True,
                 watchdog_codex_command=None):
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
            stale_after = max(30, existing["poll_interval_seconds"] * 3)
            live_watchdog = watchdog_lease_is_live(
                job_path, existing.get("watchdog_pid"), stale_after=stale_after)
            if start_watchdog and existing.get("status") in ACTIVE_STATES and not live_watchdog:
                launch_watchdog(job_path, codex_command=watchdog_codex_command)
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
            launch_watchdog(job_path, codex_command=watchdog_codex_command)
            job = load_job(job_path)
        return job, "REGISTERED"


def register_job(*args, **kwargs):
    return _register_job(*args, **kwargs)[0]
