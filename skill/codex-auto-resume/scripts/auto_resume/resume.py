import json
import os
import queue
import subprocess
import threading
import time
import uuid

from .limits import _normalize_command
from .processes import terminate_process_tree


class ResumeError(RuntimeError):
    pass


class ResumeInterrupted(ResumeError):
    def __init__(self, reason, thread_verified=False):
        super().__init__(reason)
        self.reason = reason
        self.thread_verified = thread_verified


class ResumeResult:
    def __init__(self, completed, returncode, events, final_text=None):
        self.completed = completed
        self.returncode = returncode
        self.events = events
        self.final_text = final_text


def _final_text(events):
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        pending = [event]
        examined = 0
        while pending and examined < 256:
            value = pending.pop()
            examined += 1
            if isinstance(value, dict):
                kind = value.get("type")
                if kind in {"agent_message", "message"} or "last_agent_message" in value:
                    for key in ("last_agent_message", "message", "text", "output"):
                        if isinstance(value.get(key), str) and value[key].strip():
                            return value[key].strip()
                pending.extend(reversed(list(value.values())))
            elif isinstance(value, list):
                pending.extend(reversed(value))
    return None


def validate_thread_id(thread_id):
    parsed = uuid.UUID(thread_id)
    if str(parsed) != thread_id:
        raise ValueError("thread id must be a canonical UUID")
    return thread_id


def _terminate_process_tree(proc):
    """Compatibility wrapper for existing imports and tests."""
    return terminate_process_tree(proc)


def resume_thread(codex_command, thread_id, prompt, project, env=None,
                  supervisor=None, supervisor_interval=5):
    thread_id = validate_thread_id(thread_id)
    argv = [*_normalize_command(codex_command), "exec", "resume", thread_id, prompt, "--json"]
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    process_options = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    proc = subprocess.Popen(argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=child_env,
                            shell=False, **process_options)
    events, started = [], False
    output = queue.Queue()
    stderr_lines = []

    def read_stdout():
        for value in proc.stdout:
            output.put(value)
        output.put(None)

    stdout_worker = threading.Thread(target=read_stdout, daemon=True)
    stderr_worker = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr.readlines()), daemon=True)
    stdout_worker.start()
    stderr_worker.start()
    last_supervision = time.monotonic()
    try:
        while True:
            timeout = max(0.01, supervisor_interval) if supervisor else None
            try:
                line = output.get(timeout=timeout)
            except queue.Empty:
                line = ""
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if event is not None:
                events.append(event)
                if event.get("type") == "thread.started" and not started:
                    actual = event.get("thread_id")
                    if actual != thread_id:
                        _terminate_process_tree(proc)
                        raise ResumeError(f"thread identity mismatch: expected {thread_id}, got {actual}")
                    started = True
            if supervisor and time.monotonic() - last_supervision >= supervisor_interval:
                reason = supervisor()
                last_supervision = time.monotonic()
                if reason:
                    _terminate_process_tree(proc)
                    raise ResumeInterrupted(reason, thread_verified=started)
        returncode = proc.wait()
        stderr_worker.join(timeout=1)
        if not started:
            raise ResumeError("resume produced no thread.started event")
        completed = returncode == 0 and any(item.get("type") == "turn.completed" for item in events)
        if returncode != 0:
            raise ResumeError(f"Codex resume exited with {returncode}: {''.join(stderr_lines).strip()}")
        return ResumeResult(completed, returncode, events, _final_text(events))
    finally:
        if proc.poll() is None:
            _terminate_process_tree(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
