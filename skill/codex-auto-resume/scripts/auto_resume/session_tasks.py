"""Bounded, incremental discovery of Codex turns from rollout JSONL files."""

import datetime as _datetime
import json
import math
import os
import subprocess
import time
import uuid
from pathlib import Path

from .resume import validate_thread_id
from .state import FileLock, atomic_write_json, ensure_runtime_layout, load_json

MAX_FILES = 128
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FIRST_LINE_BYTES = 1024 * 1024
LOOKBACK_SECONDS = 14 * 24 * 60 * 60
AUTO_RESUME_MARKER = "[CODEX_AUTO_RESUME]"
SUBAGENT_FALLBACK_GOAL = "Continue the assigned subagent task from its existing rollout context and checkpoint."


def _sessions_root(value=None):
    if value:
        return Path(value).expanduser().resolve()
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return home.expanduser().resolve() / "sessions"


def _event_type(row):
    payload = row.get("payload") if isinstance(row, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload["type"], payload
    return row.get("type") if isinstance(row, dict) else None, payload if isinstance(payload, dict) else {}


def _find_value(value, names):
    pending, examined = [value], 0
    while pending and examined < 4096:
        current = pending.pop(); examined += 1
        if isinstance(current, dict):
            for name in names:
                if name in current and current[name] is not None:
                    return current[name]
            pending.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))
    return None


def _walk_values(value):
    pending, examined = [value], 0
    while pending and examined < 8192:
        current = pending.pop(); examined += 1
        yield current
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _text(payload):
    value = _find_value(payload, ("message", "text", "content", "goal"))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(item if isinstance(item, str) else item.get("text", "")
                         for item in value if isinstance(item, (str, dict))).strip() or None
    return None


def _is_exhausted(payload):
    reached_names = {"rate_limit_reached", "rate_limit_reached_type", "rateLimitReached", "rateLimitReachedType"}
    percentage_names = {"used_percent", "usedPercent", "percentage", "percent_used"}
    for current in _walk_values(payload):
        if not isinstance(current, dict):
            continue
        for key, value in current.items():
            if key in reached_names and value not in (None, False, "", "none"):
                return True
            if key in percentage_names:
                try:
                    if float(value) >= 100:
                        return True
                except (TypeError, ValueError):
                    pass
    return False


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return _datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _finite_timestamp(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(float(value)))


def _canonical_or_none(value):
    try:
        return validate_thread_id(str(value)) if value else None
    except (ValueError, AttributeError):
        return None


def _new_task(task_id, at=None):
    return {"task_id": str(task_id) if task_id is not None else None, "goal": None, "cwd": None,
            "completed": False, "interrupted": False, "exhausted": False,
            "last_agent_message": None, "started_at": at, "completed_at": None,
            "input_observed": False, "launch_id": None, "launch_job_id": None,
            "launch_task_id": None, "launch_claim_state": None,
            "internal_resume": False}


def _launches_path(codex_home):
    return ensure_runtime_layout(codex_home)["state"] / "resume-launches.json"


def record_resume_launch(codex_home, job):
    """Persist intent before spawning Codex so discovery can recognize its new turn."""
    path = _launches_path(codex_home)
    launch_id = uuid.uuid4().hex
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            value = {"launches": []}
        launches = value.setdefault("launches", [])
        launches.append({
            "launch_id": launch_id, "job_id": job["job_id"], "task_id": job["task_id"],
            "thread_id": job["thread_id"], "launched_at": time.time(),
            "claim_state": "pending", "provisional_turn_id": None,
            "confirmed_turn_id": None, "observed_at": None, "closed_at": None,
        })
        cutoff = time.time() - LOOKBACK_SECONDS
        value["launches"] = [item for item in launches[-512:]
                             if float(item.get("launched_at", 0)) >= cutoff]
        atomic_write_json(path, value)
    return launch_id


def provision_resume_launch(codex_home, thread_id, resume_turn_id, started_at=None):
    """Provisionally associate a task prefix without classifying it as internal."""
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return None
        turn_id = str(resume_turn_id)
        exact = [item for item in value.get("launches", [])
                 if item.get("thread_id") == thread_id and
                 (item.get("provisional_turn_id") == turn_id or
                  item.get("confirmed_turn_id") == turn_id)]
        if len(exact) == 1:
            return dict(exact[0])
        if len(exact) > 1:
            return None
        pending = []
        for item in value.get("launches", []):
            if item.get("thread_id") != thread_id or item.get("claim_state", "pending") != "pending":
                continue
            launched_at = item.get("launched_at")
            if (not _finite_timestamp(started_at) or not _finite_timestamp(launched_at) or
                    float(started_at) < float(launched_at)):
                continue
            pending.append(item)
        if not pending:
            return None
        matched = min(pending, key=lambda item: float(item.get("launched_at", 0)))
        matched["claim_state"] = "provisional"
        matched["provisional_turn_id"] = str(resume_turn_id)
        matched["observed_at"] = time.time()
        atomic_write_json(path, value)
        return dict(matched)


def confirm_resume_launch(codex_home, launch_id, resume_turn_id):
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return None
        for item in value.get("launches", []):
            if (item.get("launch_id") == launch_id and
                    item.get("provisional_turn_id") == str(resume_turn_id) and
                    item.get("claim_state") in {"provisional", "closed", "confirmed"}):
                item["claim_state"] = "confirmed"
                item["confirmed_turn_id"] = str(resume_turn_id)
                item["observed_at"] = time.time()
                atomic_write_json(path, value)
                return dict(item)
        return None


def confirm_resume_launch_for_job(codex_home, job_id, task_id, resume_thread_id,
                                  resume_turn_id, started_at):
    """Atomically confirm one unique launch matching trusted identity and time."""
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return None
        turn_id = str(resume_turn_id)
        candidates = []
        for item in value.get("launches", []):
            if not (item.get("job_id") == job_id and item.get("task_id") == task_id and
                    item.get("thread_id") == resume_thread_id):
                continue
            state = item.get("claim_state", "pending")
            exact = (state in {"provisional", "closed", "confirmed"} and
                     (item.get("provisional_turn_id") == turn_id or
                      item.get("confirmed_turn_id") == turn_id))
            clock_match = (state == "pending" and _finite_timestamp(started_at) and
                           _finite_timestamp(item.get("launched_at")) and
                           float(started_at) >= float(item["launched_at"]))
            if exact or clock_match:
                candidates.append(item)
        if len(candidates) != 1:
            return None
        item = candidates[0]
        if (item.get("claim_state") == "confirmed" and
                item.get("provisional_turn_id") == turn_id and
                item.get("confirmed_turn_id") == turn_id):
            return dict(item)
        if item.get("claim_state", "pending") == "pending":
            item["claim_state"] = "provisional"
            item["provisional_turn_id"] = turn_id
            item["observed_at"] = time.time()
        item["claim_state"] = "confirmed"
        item["confirmed_turn_id"] = turn_id
        if item.get("observed_at") is None:
            item["observed_at"] = time.time()
        atomic_write_json(path, value)
        return dict(item)


def resume_launch_claim(codex_home, launch_id, resume_turn_id):
    """Refresh one exact turn claim while the caller holds the cursor lock."""
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return None
        for item in value.get("launches", []):
            if item.get("launch_id") != launch_id:
                continue
            turn_id = str(resume_turn_id)
            if item.get("provisional_turn_id") != turn_id:
                return None
            if (item.get("claim_state") == "confirmed" and
                    item.get("confirmed_turn_id") != turn_id):
                return None
            return dict(item)
    return None


def release_resume_launch(codex_home, launch_id, resume_turn_id):
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return False
        for item in value.get("launches", []):
            if (item.get("launch_id") == launch_id and
                    item.get("provisional_turn_id") == str(resume_turn_id) and
                    item.get("claim_state") == "provisional"):
                item["claim_state"] = "pending"
                item["provisional_turn_id"] = None
                item["observed_at"] = None
                atomic_write_json(path, value)
                return True
        return False


def close_resume_launch(codex_home, launch_id):
    """Retire an unmatched launch once its subprocess has exited."""
    path = _launches_path(codex_home)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        try:
            value = load_json(path)
        except (OSError, ValueError):
            return
        changed = False
        for item in value.get("launches", []):
            if item.get("launch_id") == launch_id and item.get("claim_state") in {"pending", "provisional"}:
                item["closed_at"] = time.time()
                item["claim_state"] = "closed"
                changed = True
        if changed:
            atomic_write_json(path, value)


def _feed_fsm(fsm, rows):
    """Consume complete rows and return task snapshots whose observable state changed."""
    emitted = []
    for row in rows:
        kind, payload = _event_type(row)
        top_level_at = _timestamp(row.get("timestamp"))
        payload_at = _timestamp(payload.get("timestamp"))
        at = top_level_at if top_level_at is not None else payload_at
        if kind == "session_meta":
            if fsm.get("meta") is None:
                fsm["meta"] = payload
                fsm["fork_timestamp"] = _timestamp(payload.get("timestamp")) or at
            continue
        if fsm.get("meta") is None:
            continue
        current = fsm.get("current")
        if kind == "task_started":
            if current is not None:
                emitted.append(dict(current))
            payload_started_at = _timestamp(_find_value(payload, ("started_at", "startedAt")))
            started_at = (top_level_at if top_level_at is not None else
                          payload_started_at if payload_started_at is not None else payload_at)
            current = _new_task(_find_value(payload, ("turn_id", "task_id", "id")),
                                started_at)
            fsm["current"] = current
            continue
        if kind == "turn_context":
            task_id = _find_value(payload, ("turn_id", "task_id", "id"))
            if current is None or (task_id is not None and current.get("task_id") != str(task_id)):
                if current is not None:
                    emitted.append(dict(current))
                current = _new_task(task_id, at); fsm["current"] = current
            cwd = _find_value(payload, ("cwd", "project_root", "project"))
            if cwd:
                current["cwd"] = str(cwd)
            continue
        if current is None:
            continue
        if kind == "user_message" and current["goal"] is None:
            current["goal"] = _text(payload)
            current["input_observed"] = current["goal"] is not None
        elif kind == "agent_message":
            message = _text(payload)
            encrypted = _find_value(payload, ("encrypted_content",)) is not None
            if current["goal"] is None and message and not encrypted:
                current["goal"] = message
                current["input_observed"] = True
            current["last_agent_message"] = message
        elif kind == "token_count":
            current["exhausted"] = current["exhausted"] or _is_exhausted(payload)
        elif kind in {"task_complete", "turn_complete", "turn_completed"}:
            current["completed"] = True
            current["completed_at"] = _timestamp(_find_value(payload, ("completed_at", "completedAt"))) or at
            if "last_agent_message" in payload:
                current["last_agent_message"] = payload.get("last_agent_message")
            current["interrupted"] = bool(current["exhausted"] and current["last_agent_message"] is None)
            emitted.append(dict(current))
    current = fsm.get("current")
    if current is not None:
        if not current["completed"] and current["exhausted"]:
            current["interrupted"] = True
        emitted.append(dict(current))
    return emitted


def _format_tasks(fsm, tasks, rollout_path):
    meta = fsm.get("meta")
    if not isinstance(meta, dict):
        return []
    thread_id = _canonical_or_none(meta.get("id"))
    if thread_id is None:
        return []
    parent_thread_id = _canonical_or_none(_find_value(meta, ("parent_thread_id", "parentThreadId", "forked_from_id")))
    root_thread_id = _canonical_or_none(_find_value(meta, ("root_thread_id", "rootThreadId")))
    base_cwd = _find_value(meta, ("cwd", "project_root", "project"))
    parent_task_id = _find_value(meta, ("parent_task_id", "parentTaskId"))
    agent_path = _find_value(meta, ("agent_path", "agentPath"))
    result = []
    for task in tasks:
        if not task.get("task_id"):
            continue
        goal = task.get("goal")
        result.append({
            "thread_id": thread_id, "task_id": task["task_id"],
            "thread_source": str(meta.get("thread_source") or "rollout"),
            "parent_thread_id": parent_thread_id,
            "parent_task_id": str(parent_task_id) if parent_task_id is not None else None,
            "root_thread_id": root_thread_id or parent_thread_id or thread_id,
            "agent_path": str(agent_path) if agent_path is not None else None,
            "rollout_path": str(Path(rollout_path).resolve()),
            "cwd": task.get("cwd") or (str(base_cwd) if base_cwd else None),
            "goal": goal, "goal_source": "user_message" if parent_thread_id is None else "agent_message",
            "completed": task["completed"], "interrupted": task["interrupted"],
            "internal_resume": bool(task.get("internal_resume") or
                                    task.get("launch_claim_state") == "confirmed"),
            "input_observed": bool(task.get("input_observed")),
            "launch_id": task.get("launch_id"),
            "launch_job_id": task.get("launch_job_id"),
            "launch_task_id": task.get("launch_task_id"),
            "launch_claim_state": task.get("launch_claim_state"),
            "last_agent_message": task.get("last_agent_message"),
            "started_at": task.get("started_at"), "completed_at": task.get("completed_at"),
            "fork_timestamp": fsm.get("fork_timestamp"),
        })
    return result


def _parse_rows(rows, rollout_path):
    fsm = {"meta": None, "current": None, "fork_timestamp": None}
    return _format_tasks(fsm, _feed_fsm(fsm, rows), rollout_path)


def _decode_rows(data):
    rows = []
    for raw in data.splitlines():
        if not raw or len(raw) > MAX_FIRST_LINE_BYTES:
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _initial_rows(path):
    """Bootstrap a file. Large files only backtrack within the bounded tail."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        first = handle.readline(MAX_FIRST_LINE_BYTES + 1)
        if size <= MAX_FILE_BYTES:
            handle.seek(0); data = handle.read(MAX_FILE_BYTES); start = 0
        else:
            start = size - MAX_FILE_BYTES; handle.seek(start); data = handle.read(MAX_FILE_BYTES)
    first_rows = _decode_rows(first) if first.endswith(b"\n") and len(first) <= MAX_FIRST_LINE_BYTES else []
    if start:
        _, sep, data = data.partition(b"\n")
        if not sep:
            data = b""
        lines = data.splitlines(keepends=True)
        boundary = None
        for index, raw in enumerate(lines):
            parsed = _decode_rows(raw)
            if parsed and _event_type(parsed[0])[0] in {"task_started", "turn_context"}:
                boundary = index
        if boundary is None:
            return first_rows, size, b"", True
        data = b"".join(lines[boundary:])
    complete, sep, remainder = data.rpartition(b"\n")
    rows = _decode_rows(complete) if sep else []
    if start and first_rows:
        rows = first_rows + rows
    return rows, size, remainder if sep else data[-MAX_FIRST_LINE_BYTES:], False


def _increment_rows(path, offset, prefix):
    with path.open("rb") as handle:
        handle.seek(offset); chunk = handle.read(MAX_FILE_BYTES); end = handle.tell()
    data = prefix + chunk
    complete, sep, remainder = data.rpartition(b"\n")
    return (_decode_rows(complete) if sep else [], end,
            remainder if sep else data[-MAX_FIRST_LINE_BYTES:])


def _candidate_rollouts(root, thread_id=None, max_files=MAX_FILES):
    if not root.is_dir(): return []
    cutoff, candidates, visited = time.time() - LOOKBACK_SECONDS, [], 0
    for directory, directories, files in os.walk(root):
        directories.sort(reverse=True)
        for name in sorted(files, reverse=True):
            if not name.startswith("rollout-") or not name.endswith(".jsonl"): continue
            visited += 1
            if visited > max_files * 8: break
            path = Path(directory) / name
            try: stat = path.stat()
            except OSError: continue
            if stat.st_mtime < cutoff and thread_id is None: continue
            if thread_id and thread_id not in name: continue
            candidates.append((stat.st_mtime, path))
        if visited > max_files * 8: break
    candidates.sort(reverse=True)
    return [path for _, path in candidates[:max_files]]


def resolve_current_task(thread_id=None, sessions_root=None):
    thread_id = validate_thread_id(str(thread_id or os.environ.get("CODEX_THREAD_ID", "")))
    for path in _candidate_rollouts(_sessions_root(sessions_root), thread_id=thread_id):
        rows, _, _, deferred = _initial_rows(path)
        if deferred: continue
        parsed = _parse_rows(rows, path)
        if parsed and parsed[0]["thread_id"] == thread_id: return parsed[-1]
    return None


def task_intervals_for_thread(thread_id, sessions_root=None):
    result = []
    for path in _candidate_rollouts(_sessions_root(sessions_root), thread_id=thread_id):
        rows, _, _, deferred = _initial_rows(path)
        if deferred: continue
        result.extend(_parse_rows(rows, path))
    result.sort(key=lambda task: task.get("started_at") or float("-inf"))
    for index, task in enumerate(result[:-1]):
        if task.get("completed_at") is None:
            task["completed_at"] = result[index + 1].get("started_at")
    return result


def discover_session_updates(codex_home=None, sessions_root=None, max_files=MAX_FILES,
                             max_total_bytes=MAX_TOTAL_BYTES):
    layout = ensure_runtime_layout(codex_home)
    cursor_path, lock_path = layout["state"] / "session-cursors.json", layout["state"] / "session-cursors.lock"
    with FileLock(lock_path, timeout=5):
        try: cursors = load_json(cursor_path)
        except (OSError, ValueError): cursors = {"files": {}, "seen": []}
        file_state, seen = cursors.setdefault("files", {}), set(cursors.setdefault("seen", []))
        tasks, completed, errors, deferred, total = [], [], [], 0, 0
        effective = sessions_root or (Path(codex_home).expanduser().resolve() / "sessions" if codex_home else None)
        for path in _candidate_rollouts(_sessions_root(effective), max_files=max_files):
            key, previous = str(path.resolve()), file_state.get(str(path.resolve()), {})
            try:
                stat = path.stat(); identity = f"{getattr(stat, 'st_dev', 0)}:{getattr(stat, 'st_ino', 0)}"
                rotated = previous.get("identity") not in (None, identity) or stat.st_size < int(previous.get("offset", 0))
                if not rotated and stat.st_size == int(previous.get("offset", 0)): continue
                if total >= max_total_bytes: break
                fsm = previous.get("fsm") if not rotated and isinstance(previous.get("fsm"), dict) else {"meta": None, "current": None, "fork_timestamp": None}
                if rotated or not previous:
                    rows, offset, remainder, was_deferred = _initial_rows(path)
                    total += min(stat.st_size, MAX_FILE_BYTES)
                else:
                    prefix = bytes.fromhex(previous.get("remainder_hex", ""))
                    rows, offset, remainder = _increment_rows(path, int(previous.get("offset", 0)), prefix)
                    total += offset - int(previous.get("offset", 0))
                    was_deferred = bool(previous.get("deferred"))
                parsed = _format_tasks(fsm, _feed_fsm(fsm, rows), path)
                if fsm.get("current") is not None:
                    was_deferred = False
                deferred += int(was_deferred)
                for task in parsed:
                    current = fsm.get("current", {}) if fsm.get("current", {}).get("task_id") == task["task_id"] else None
                    if not task.get("launch_id"):
                        launch = provision_resume_launch(codex_home, task["thread_id"], task["task_id"],
                                                         task.get("started_at"))
                        if launch:
                            claim_state = launch.get("claim_state", "provisional")
                            internal_resume = bool(
                                claim_state == "confirmed" and
                                launch.get("confirmed_turn_id") == task["task_id"])
                            for field, value in (("launch_id", launch["launch_id"]),
                                                 ("launch_job_id", launch["job_id"]),
                                                 ("launch_task_id", launch["task_id"]),
                                                 ("launch_claim_state", claim_state),
                                                 ("internal_resume", internal_resume)):
                                task[field] = value
                                if current is not None:
                                    current[field] = value
                    if task.get("launch_id"):
                        claim = resume_launch_claim(codex_home, task["launch_id"], task["task_id"])
                        if claim:
                            refreshed = {
                                "launch_job_id": claim.get("job_id"),
                                "launch_task_id": claim.get("task_id"),
                                "launch_claim_state": claim.get("claim_state"),
                                "internal_resume": bool(
                                    claim.get("claim_state") == "confirmed" and
                                    claim.get("confirmed_turn_id") == task["task_id"]),
                            }
                            task.update(refreshed)
                            if current is not None:
                                current.update(refreshed)
                    if not task["input_observed"]:
                        if (not task.get("internal_resume") and
                                task.get("launch_claim_state") in {"provisional", "closed"}):
                            continue
                        if not task["completed"] and not task.get("internal_resume"):
                            # Prefixes, including provisional launch claims,
                            # remain reconsiderable and never enter seen.
                            continue
                    elif task.get("launch_id"):
                        if task.get("internal_resume"):
                            pass
                        elif task.get("goal") and AUTO_RESUME_MARKER in task["goal"]:
                            launch = confirm_resume_launch(codex_home, task["launch_id"], task["task_id"])
                            task["launch_claim_state"] = "confirmed" if launch else task.get("launch_claim_state")
                            task["internal_resume"] = bool(launch)
                            if current is not None:
                                current["launch_claim_state"] = task["launch_claim_state"]
                                current["internal_resume"] = task["internal_resume"]
                        else:
                            release_resume_launch(codex_home, task["launch_id"], task["task_id"])
                            for field in ("launch_id", "launch_job_id", "launch_task_id", "launch_claim_state"):
                                task[field] = None
                                if current is not None:
                                    current[field] = None
                            task["internal_resume"] = False
                    signature = f"{task['thread_id']}:{task['task_id']}:{task['completed']}:{task['interrupted']}"
                    if signature in seen: continue
                    seen.add(signature)
                    if task["completed"] and not task["interrupted"]: completed.append(task)
                    elif not task["internal_resume"]: tasks.append(task)
                file_state[key] = {"identity": identity, "offset": offset, "remainder_hex": remainder.hex(),
                                   "mtime_ns": stat.st_mtime_ns, "fsm": fsm, "deferred": was_deferred}
            except (OSError, ValueError) as exc:
                errors.append({"rollout": key, "error": str(exc)})
        cursors["seen"] = sorted(seen)[-4096:]
        atomic_write_json(cursor_path, cursors)
    return {"tasks": tasks, "completed": completed, "errors": errors,
            "examined": len(file_state), "deferred": deferred}


def git_root(cwd):
    from .workspace import git_root as resolve_git_root
    return resolve_git_root(cwd)
