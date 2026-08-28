import argparse
import json
import os
import sys
from pathlib import Path

from .core import InstallError, doctor, install, uninstall


def build_parser():
    parser = argparse.ArgumentParser(prog="codex-auto-resume", description="Install and manage Codex Auto Resume")
    parser.add_argument("command", nargs="?", default="install", choices=("install", "doctor", "uninstall"))
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--disable-default-activation", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--platform", choices=("win32", "darwin", "linux"), help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(__file__).parents[2]
    simulate = os.environ.get("CODEX_AUTO_RESUME_SIMULATE") == "1"
    skip = os.environ.get("CODEX_AUTO_RESUME_SKIP_PREREQUISITES") == "1"
    try:
        if args.command == "install":
            result = install(
                root, args.codex_home, platform_name=args.platform, simulate=simulate,
                skip_prerequisites=skip, adopt_existing=args.adopt_existing,
                disable_default_activation=args.disable_default_activation,
            )
        elif args.command == "doctor":
            result = doctor(args.codex_home, platform_name=args.platform, simulate=simulate,
                            skip_prerequisites=skip)
        else:
            result = uninstall(args.codex_home, platform_name=args.platform, simulate=simulate,
                               purge_data=args.purge_data)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except (InstallError, OSError, RuntimeError) as exc:
        print(f"codex-auto-resume: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
