#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.checkpoints import HEADINGS, read_checkpoint, write_checkpoint
from auto_resume.repo import fingerprint
from auto_resume.state import load_job, runtime_home, save_job


def main(argv=None):
    parser = argparse.ArgumentParser(description="原子更新 Codex 自动续作检查点")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="HEADING=VALUE")
    args = parser.parse_args(argv)
    job_path = runtime_home(args.codex_home) / "jobs" / f"{args.job_id}.json"
    job = load_job(job_path)
    values = read_checkpoint(job["checkpoint_path"])
    for assignment in args.set:
        if "=" not in assignment:
            parser.error("--set requires HEADING=VALUE")
        key, value = assignment.split("=", 1)
        if key not in HEADINGS:
            parser.error(f"unknown heading: {key}")
        values[key] = value
    write_checkpoint(job["checkpoint_path"], values)
    job["expected_repo_snapshot"] = fingerprint(job["project_root"])
    if values["AUTO_RESUME_STATUS"].strip().upper() == "DONE":
        job["status"] = "DONE"
    save_job(job_path, job)
    print(json.dumps({"job_id": job["job_id"], "status": job["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
