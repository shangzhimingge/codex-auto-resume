#!/usr/bin/env python3
"""Small Codex CLI fixture controlled through environment variables."""
import json
import os
import sys
import time
import subprocess


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
    env_record = os.environ.get("FAKE_ENV_LOG")
    if env_record:
        with open(env_record, "w", encoding="utf-8") as handle:
            json.dump({key: os.environ.get(key) for key in (
                "CODEX_AUTO_RESUME_JOB_ID", "CODEX_AUTO_RESUME_TASK_ID")}, handle)
    thread_id = os.environ.get("FAKE_THREAD_ID", sys.argv[3])
    emit({"type": "thread.started", "thread_id": thread_id})
    checkpoint_command = os.environ.get("FAKE_CHECKPOINT_COMMAND_JSON")
    if checkpoint_command:
        subprocess.run(json.loads(checkpoint_command), check=True)
    signal_path = os.environ.get("FAKE_AFTER_CHECKPOINT_SIGNAL")
    if signal_path:
        with open(signal_path, "w", encoding="utf-8") as handle:
            handle.write("ready")
    release_path = os.environ.get("FAKE_RESULT_RELEASE")
    if release_path:
        deadline = time.monotonic() + 10
        while not os.path.exists(release_path):
            if time.monotonic() >= deadline:
                return 3
            time.sleep(0.01)
    time.sleep(float(os.environ.get("FAKE_RESUME_SLEEP", "0")))
    message = os.environ.get("FAKE_AGENT_MESSAGE")
    if message:
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
        artifact = os.environ.get("FAKE_ARTIFACT_PATH")
        if artifact:
            event["item"]["artifact_path"] = artifact
        emit(event)
    emit({"type": os.environ.get("FAKE_FINAL_EVENT", "turn.completed")})
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["app-server"]:
        app_server()
        raise SystemExit(0)
    if sys.argv[1:3] == ["exec", "resume"]:
        raise SystemExit(exec_resume())
    raise SystemExit(2)
