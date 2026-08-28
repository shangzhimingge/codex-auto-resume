from pathlib import Path

from .registering import _register_job
from .repo import validate_repo
from .resume import validate_thread_id


def preflight(thread_id=None, project=None, goal=None, codex_home=None, opt_out=False,
              start_watchdog=True, max_cycles=None):
    if opt_out:
        return {"outcome": "SKIPPED", "reason": "explicit_opt_out"}
    if not thread_id or not project or not goal or not str(goal).strip():
        return {"outcome": "SKIPPED", "reason": "missing_required_context"}
    try:
        thread_id = validate_thread_id(str(thread_id))
        project = validate_repo(Path(project))
    except (ValueError, RuntimeError, OSError):
        return {"outcome": "SKIPPED", "reason": "ineligible_context"}
    job, outcome = _register_job(thread_id, project, str(goal), codex_home,
                                 max_cycles=max_cycles, start_watchdog=start_watchdog)
    return {"outcome": outcome, "job": job}
