#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.registering import register_job


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description="注册 Codex 自动续作任务")
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--max-cycles", type=positive_int, default=None)
    parser.add_argument("--poll-interval", type=int, default=60)
    parser.add_argument("--safety-margin", type=int, default=30)
    parser.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    job = register_job(args.thread_id, args.project, args.goal, args.codex_home,
                       args.max_cycles, args.poll_interval, args.safety_margin,
                       start_watchdog=not args.no_start)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
