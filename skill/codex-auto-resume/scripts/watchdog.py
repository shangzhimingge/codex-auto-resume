#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.limits import read_limits, reset_deadline
from auto_resume.registering import start_watchdog
from auto_resume.state import load_job, runtime_home
from auto_resume.watch import run_job


def _job_path(value, home):
    path = Path(value)
    return path.resolve() if path.exists() else runtime_home(home) / "jobs" / f"{value}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex 自动续作守护进程")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--job", required=True)
    start.add_argument("--codex-home", type=Path)
    start.add_argument("--once", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--job", required=True)
    run.add_argument("--codex-home", type=Path)
    run.add_argument("--once", action="store_true")
    run.add_argument("--nonce", help=argparse.SUPPRESS)
    run.add_argument("--codex-command-json", help=argparse.SUPPRESS)
    status = sub.add_parser("status")
    status.add_argument("--job", required=True)
    status.add_argument("--codex-home", type=Path)
    sub.add_parser("probe-limits")
    args = parser.parse_args(argv)
    if args.command == "probe-limits":
        snapshot = read_limits()
        print(json.dumps({"limit_id": snapshot.limit_id, "buckets": snapshot.buckets,
                          "reset_deadline": reset_deadline(snapshot, __import__("time").time())},
                         ensure_ascii=False, indent=2))
        return 0
    job_path = _job_path(args.job, args.codex_home)
    if args.command == "status":
        print(json.dumps(load_job(job_path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        print(json.dumps({"watchdog_pid": start_watchdog(job_path)}))
        return 0
    codex_command = tuple(json.loads(args.codex_command_json)) if args.codex_command_json else ("codex",)
    print(run_job(job_path, codex_command=codex_command, once=args.once, nonce=args.nonce))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
