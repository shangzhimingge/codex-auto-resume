#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.daemon import daemon_status, run_daemon, stop_daemon


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex Auto Resume service daemon")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--codex-home", type=Path)
    run.add_argument("--interval", type=int, default=10)
    run.add_argument("--once", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--codex-home", type=Path)
    stop = sub.add_parser("stop")
    stop.add_argument("--codex-home", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(daemon_status(args.codex_home), ensure_ascii=False, indent=2))
    elif args.command == "stop":
        print(json.dumps(stop_daemon(args.codex_home), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_daemon(args.codex_home, args.interval, args.once), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
