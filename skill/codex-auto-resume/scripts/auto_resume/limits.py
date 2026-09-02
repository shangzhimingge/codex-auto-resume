import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import __version__
from .processes import terminate_process_tree


class LimitsError(RuntimeError):
    pass


class LimitsSnapshot:
    def __init__(self, limit_id, buckets, raw, reached_type=None):
        self.limit_id = limit_id
        self.buckets = buckets
        self.raw = raw
        self.reached_type = reached_type


def _normalize_command(codex_command):
    command = list(codex_command)
    if os.name == "nt" and command == ["codex"]:
        wrapper = shutil.which("codex.cmd")
        if wrapper:
            entrypoint = Path(wrapper).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node.exe")
            if node and entrypoint.is_file():
                return [node, str(entrypoint)]
        native = shutil.which("codex.exe")
        if native:
            return [native]
    return command


def _readline_with_timeout(stream, timeout, workers=None):
    box = []

    def read_one():
        try:
            box.append(stream.readline())
        except (OSError, ValueError):
            box.append("")

    worker = threading.Thread(target=read_one, daemon=True)
    if workers is not None:
        workers.append(worker)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not box or not box[0]:
        raise LimitsError("app-server response timeout")
    return box[0]


def _request(proc, message, timeout, workers=None):
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    expected = message.get("id")
    while True:
        try:
            response = json.loads(_readline_with_timeout(proc.stdout, timeout, workers))
        except (ValueError, json.JSONDecodeError) as exc:
            raise LimitsError("malformed app-server JSON") from exc
        if response.get("id") == expected:
            if "error" in response:
                raise LimitsError(f"app-server error: {response['error']}")
            return response.get("result")


def read_limits(codex_command=("codex",), timeout=15):
    argv = [*_normalize_command(codex_command), "app-server", "--listen", "stdio://"]
    proc = None
    workers = []
    try:
        process_options = {"close_fds": True, "shell": False}
        if os.name == "nt":
            process_options["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            process_options["start_new_session"] = True
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", **process_options,
        )
        init = {"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "codex-auto-resume", "title": "Codex Auto Resume", "version": __version__},
            "capabilities": {"experimentalApi": True},
        }}
        _request(proc, init, timeout, workers)
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        result = _request(proc, {"method": "account/rateLimits/read", "id": 2}, timeout,
                          workers)
    except (OSError, BrokenPipeError) as exc:
        action = "failed to start" if proc is None else "communication failed"
        raise LimitsError(f"app-server {action}: {exc}") from exc
    finally:
        if proc is not None:
            terminate_process_tree(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for worker in workers:
                worker.join(timeout=2)
    if not isinstance(result, dict):
        raise LimitsError("missing rate-limit result")
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        selected, limit_id = by_id["codex"], "codex"
    elif isinstance(result.get("rateLimits"), dict):
        selected, limit_id = result["rateLimits"], "legacy"
    else:
        raise LimitsError("Codex rate-limit bucket is missing")
    buckets = []
    for name in ("primary", "secondary"):
        bucket = selected.get(name)
        if bucket is None:
            continue
        if not isinstance(bucket, dict):
            raise LimitsError(f"malformed {name} bucket")
        used, reset = bucket.get("usedPercent"), bucket.get("resetsAt")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            raise LimitsError(f"malformed {name}.usedPercent")
        if reset is not None and (not isinstance(reset, (int, float)) or isinstance(reset, bool)):
            raise LimitsError(f"malformed {name}.resetsAt")
        buckets.append({"name": name, "used_percent": float(used), "resets_at": reset})
    if not buckets:
        raise LimitsError("no rate-limit windows found")
    return LimitsSnapshot(limit_id, buckets, result, selected.get("rateLimitReachedType"))


def reset_deadline(snapshot, now):
    exhausted = [item for item in snapshot.buckets if item["used_percent"] >= 100]
    if snapshot.reached_type is not None:
        exhausted = list(snapshot.buckets)
    if not exhausted:
        return None
    resets = [item["resets_at"] for item in exhausted]
    if any(value is None for value in resets):
        raise LimitsError("exhausted window has no reset timestamp")
    return max(float(now), max(resets))
