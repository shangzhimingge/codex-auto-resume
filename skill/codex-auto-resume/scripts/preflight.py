#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.activation import preflight


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行每任务自动续作预检")
    parser.add_argument("--thread-id")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--goal")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--opt-out", action="store_true")
    parser.add_argument("--max-cycles", type=positive_int)
    parser.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    result = preflight(args.thread_id, args.project, args.goal, args.codex_home,
                       args.opt_out, start_watchdog=not args.no_start,
                       max_cycles=args.max_cycles)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
