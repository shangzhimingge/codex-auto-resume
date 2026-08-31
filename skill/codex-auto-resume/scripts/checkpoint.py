#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auto_resume.checkpoints import HEADINGS, read_checkpoint, write_checkpoint
from auto_resume.repo import fingerprint
from auto_resume.state import ACTIVE_STATES, load_job, runtime_home, update_job
from auto_resume.watch import (_advance_lineage, _project_lock, _publish_child_terminal_locked,
                               _stage_child_handoff)
from auto_resume.state import FileLock


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
    with FileLock(_project_lock(job, args.codex_home), timeout=10):
        write_checkpoint(job["checkpoint_path"], values)
        snapshot = fingerprint(job["project_root"])
        done = values["AUTO_RESUME_STATUS"].strip().upper() == "DONE"
        internal_child = bool(
            done and job.get("parent_thread_id") and
            os.environ.get("CODEX_AUTO_RESUME_JOB_ID") == job["job_id"] and
            os.environ.get("CODEX_AUTO_RESUME_TASK_ID") == job["task_id"])
        if internal_child:
            job = update_job(job_path, lambda value: value.update(expected_repo_snapshot=snapshot))
            _stage_child_handoff(job, args.codex_home, "DONE")
            _advance_lineage(job, args.codex_home, snapshot)
        elif done and job.get("parent_thread_id"):
            job = _publish_child_terminal_locked(
                job_path, args.codex_home, "NEEDS_USER", snapshot=snapshot,
                error="child checkpoint completed without a resumable final result")
        else:
            job = update_job(job_path, lambda value: value.update(
                expected_repo_snapshot=snapshot,
                status="DONE" if done and value.get("status") in ACTIVE_STATES else value.get("status")))
            _advance_lineage(job, args.codex_home, snapshot)
    print(json.dumps({"job_id": job["job_id"], "status": job["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
