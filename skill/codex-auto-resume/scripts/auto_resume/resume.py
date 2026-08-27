import json
import os
import subprocess
import uuid

from .limits import _normalize_command


class ResumeError(RuntimeError):
    pass


class ResumeResult:
    def __init__(self, completed, returncode, events):
        self.completed = completed
        self.returncode = returncode
        self.events = events


def validate_thread_id(thread_id):
    parsed = uuid.UUID(thread_id)
    if str(parsed) != thread_id.lower():
        raise ValueError("thread id must be a canonical UUID")
    return str(parsed)


def resume_thread(codex_command, thread_id, prompt, project, env=None):
    thread_id = validate_thread_id(thread_id)
    argv = [*_normalize_command(codex_command), "exec", "resume", thread_id, prompt, "--json"]
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    proc = subprocess.Popen(argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=child_env,
                            shell=False)
    events, started = [], False
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get("type") == "thread.started" and not started:
                actual = event.get("thread_id")
                if actual != thread_id:
                    proc.terminate()
                    raise ResumeError(f"thread identity mismatch: expected {thread_id}, got {actual}")
                started = True
        returncode = proc.wait()
        if not started:
            raise ResumeError("resume produced no thread.started event")
        completed = returncode == 0 and any(item.get("type") == "turn.completed" for item in events)
        if returncode != 0:
            raise ResumeError(f"Codex resume exited with {returncode}: {proc.stderr.read().strip()}")
        return ResumeResult(completed, returncode, events)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
