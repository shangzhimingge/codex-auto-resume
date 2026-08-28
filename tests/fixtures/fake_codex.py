#!/usr/bin/env python3
"""Small Codex CLI fixture controlled through environment variables."""
import json
import os
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


def app_server():
    messages = []
    for line in sys.stdin:
        message = json.loads(line)
        messages.append(message)
        record = os.environ.get("FAKE_RPC_LOG")
        if record:
            with open(record, "w", encoding="utf-8") as handle:
                json.dump(messages, handle)
        method = message.get("method")
        if method == "initialize":
            emit({"id": message["id"], "result": {"userAgent": "fake"}})
        elif method == "account/rateLimits/read":
            raw = os.environ.get("FAKE_LIMITS", '{"rateLimits":null}')
            result = json.loads(raw)
            emit({"id": message["id"], "result": result})


def exec_resume():
    record = os.environ.get("FAKE_ARGV_LOG")
    if record:
        with open(record, "w", encoding="utf-8") as handle:
            json.dump(sys.argv[1:], handle)
    thread_id = os.environ.get("FAKE_THREAD_ID", sys.argv[3])
    emit({"type": "thread.started", "thread_id": thread_id})
    time.sleep(float(os.environ.get("FAKE_RESUME_SLEEP", "0")))
    emit({"type": os.environ.get("FAKE_FINAL_EVENT", "turn.completed")})
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["app-server"]:
        app_server()
        raise SystemExit(0)
    if sys.argv[1:3] == ["exec", "resume"]:
        raise SystemExit(exec_resume())
    raise SystemExit(2)
