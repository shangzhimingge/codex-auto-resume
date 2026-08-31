import argparse
import json
import time
from pathlib import Path

from .activation import preflight
from .daemon import daemon_status, run_daemon, scan_once, stop_daemon
from .limits import read_limits, reset_deadline
from .registering import start_watchdog
from .state import load_job, runtime_home
from .watch import run_job


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _job_path(value, home):
    path = Path(value)
    return path.resolve() if path.exists() else runtime_home(home) / "jobs" / f"{value}.json"


def build_parser():
    parser = argparse.ArgumentParser(description="Codex Auto Resume unified runtime interface")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight")
    pre.add_argument("--thread-id")
    pre.add_argument("--task-id")
    pre.add_argument("--project", type=Path)
    pre.add_argument("--goal")
    pre.add_argument("--codex-home", type=Path)
    pre.add_argument("--opt-out", action="store_true")
    pre.add_argument("--max-cycles", type=positive_int)
    pre.add_argument("--parent-thread-id")
    pre.add_argument("--parent-task-id")
    pre.add_argument("--root-thread-id")
    pre.add_argument("--agent-path")
    pre.add_argument("--rollout-path", type=Path)
    pre.add_argument("--fork-timestamp", type=float)
    pre.add_argument("--association-source")
    pre.add_argument("--sessions-root", type=Path, help=argparse.SUPPRESS)
    pre.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)

    job = sub.add_parser("run-job")
    job.add_argument("--job", required=True)
    job.add_argument("--codex-home", type=Path)
    job.add_argument("--once", action="store_true")
    job.add_argument("--nonce", help=argparse.SUPPRESS)
    job.add_argument("--codex-command-json", help=argparse.SUPPRESS)

    daemon = sub.add_parser("daemon")
    daemon.add_argument("action", choices=("run", "status", "stop", "scan"))
    daemon.add_argument("--codex-home", type=Path)
    daemon.add_argument("--interval", type=positive_int, default=10)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--sessions-root", type=Path, help=argparse.SUPPRESS)

    probe = sub.add_parser("probe-limits")
    probe.add_argument("--timeout", type=positive_int, default=15)

    status = sub.add_parser("status")
    status.add_argument("--job")
    status.add_argument("--codex-home", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "preflight":
        result = preflight(
            args.thread_id, args.project, args.goal, args.codex_home, args.opt_out,
            start_watchdog=not args.no_start, max_cycles=args.max_cycles,
            task_id=args.task_id, parent_thread_id=args.parent_thread_id,
            parent_task_id=args.parent_task_id, root_thread_id=args.root_thread_id,
            agent_path=args.agent_path, rollout_path=args.rollout_path,
            sessions_root=args.sessions_root,
            fork_timestamp=args.fork_timestamp, association_source=args.association_source,
        )
    elif args.command == "run-job":
        codex = tuple(json.loads(args.codex_command_json)) if args.codex_command_json else ("codex",)
        result = run_job(_job_path(args.job, args.codex_home), codex_command=codex,
                         once=args.once, nonce=args.nonce)
    elif args.command == "daemon":
        if args.action == "run":
            result = run_daemon(args.codex_home, args.interval, args.once, sessions_root=args.sessions_root)
        elif args.action == "status":
            result = daemon_status(args.codex_home)
        elif args.action == "stop":
            result = stop_daemon(args.codex_home)
        else:
            result = scan_once(args.codex_home, sessions_root=args.sessions_root)
    elif args.command == "probe-limits":
        snapshot = read_limits(timeout=args.timeout)
        result = {
            "limit_id": snapshot.limit_id,
            "buckets": snapshot.buckets,
            "reset_deadline": reset_deadline(snapshot, time.time()),
        }
    elif args.job:
        result = load_job(_job_path(args.job, args.codex_home))
    else:
        result = daemon_status(args.codex_home)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
