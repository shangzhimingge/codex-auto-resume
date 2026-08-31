"""Durable revisioned child-to-parent result handoffs."""

import hashlib
import json
from pathlib import Path

from .state import FileLock, atomic_write_json, ensure_runtime_layout, load_json, utc_now


def _key(value):
    fields = (value.get("parent_thread_id"), value.get("parent_task_id"),
              value.get("child_thread_id"), value.get("child_task_id"))
    if any(item in (None, "") for item in fields):
        raise ValueError("handoff lineage is incomplete")
    return hashlib.sha256("\0".join(map(str, fields)).encode("utf-8")).hexdigest()[:32]


def _path(codex_home, value):
    return ensure_runtime_layout(codex_home)["handoffs"] / f"{_key(value)}.json"


def _normalized(document):
    value = dict(document)
    value.setdefault("agent_path", None)
    value.setdefault("final_text", None)
    value.setdefault("event_summary", [])
    value.setdefault("artifacts", [])
    value.setdefault("consumed", False)
    value.setdefault("consumed_at", None)
    if "finalized" not in value:
        value["finalized"] = bool(value.get("final_text")) or value.get("status") != "DONE"
    value.setdefault("revision", 1 if value["finalized"] else 0)
    value.setdefault("created_at", utc_now())
    return value


def _merge_list(first, second):
    merged, seen = [], set()
    for item in [*(first or []), *(second or [])]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key); merged.append(item)
    return merged


def stage_handoff(codex_home, value):
    path = _path(codex_home, value)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        if path.exists():
            document = _normalized(load_json(path))
            if document.get("finalized") or document.get("consumed"):
                return path
            for field in ("agent_path", "status"):
                if not document.get(field) and value.get(field):
                    document[field] = value[field]
            document["event_summary"] = _merge_list(document.get("event_summary"),
                                                      value.get("event_summary"))
            document["artifacts"] = _merge_list(document.get("artifacts"), value.get("artifacts"))
            atomic_write_json(path, document)
            return path
        document = _normalized(value)
        document["finalized"] = False
        document["revision"] = 0
        document["consumed"] = False
        document["consumed_at"] = None
        atomic_write_json(path, document)
    return path


def finalize_handoff(codex_home, value):
    path = _path(codex_home, value)
    with FileLock(path.with_suffix(".lock"), timeout=5):
        if path.exists():
            document = _normalized(load_json(path))
        else:
            document = _normalized(value)
            document["finalized"] = False
            document["revision"] = 0
            document["consumed"] = False
            document["consumed_at"] = None
        if document.get("finalized"):
            return {**document, "path": str(path.resolve())}
        status = value.get("status") or document.get("status")
        final_text = value.get("final_text") or document.get("final_text")
        if status == "DONE" and (not isinstance(final_text, str) or not final_text.strip()):
            raise ValueError("DONE handoff requires final_text")
        document["status"] = status
        document["final_text"] = final_text
        if value.get("agent_path") is not None:
            document["agent_path"] = value["agent_path"]
        document["event_summary"] = _merge_list(document.get("event_summary"),
                                                  value.get("event_summary"))
        document["artifacts"] = _merge_list(document.get("artifacts"), value.get("artifacts"))
        document["revision"] = int(document.get("revision", 0)) + 1
        document["finalized"] = True
        document["finalized_at"] = utc_now()
        atomic_write_json(path, document)
        return {**document, "path": str(path.resolve())}


def write_handoff(codex_home, value):
    """Backward-compatible publication: incomplete DONE values are staged."""
    if value.get("status") != "DONE" or value.get("final_text"):
        return Path(finalize_handoff(codex_home, value)["path"])
    return stage_handoff(codex_home, value)


def pending_handoffs(codex_home, parent_thread_id, parent_task_id):
    layout = ensure_runtime_layout(codex_home)
    result = []
    for path in sorted(layout["handoffs"].glob("*.json")):
        try:
            value = _normalized(load_json(path))
        except (OSError, ValueError):
            continue
        if (value.get("parent_thread_id") == parent_thread_id and
                value.get("parent_task_id") == parent_task_id and
                value.get("finalized") and not value.get("consumed")):
            result.append({**value, "path": str(path.resolve())})
    return result


def consume_handoffs(codex_home, parent_thread_id, parent_task_id, receipts=None):
    selected = {str(Path(path).resolve()): int(revision) for path, revision in (receipts or [])}
    if not selected:
        return []
    consumed = []
    for handoff in pending_handoffs(codex_home, parent_thread_id, parent_task_id):
        path = Path(handoff["path"])
        expected_revision = selected.get(str(path.resolve()))
        if expected_revision is None:
            continue
        with FileLock(path.with_suffix(".lock"), timeout=5):
            value = _normalized(load_json(path))
            if (value.get("consumed") or not value.get("finalized") or
                    int(value.get("revision", 0)) != expected_revision):
                continue
            value["consumed"] = True
            value["consumed_at"] = utc_now()
            atomic_write_json(path, value)
            consumed.append(str(path.resolve()))
    return consumed
