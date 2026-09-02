import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


WORKSPACE_KINDS = {"git", "directory", "managed"}


@dataclass(frozen=True)
class Workspace:
    kind: str
    root: Path

    def __post_init__(self):
        if self.kind not in WORKSPACE_KINDS:
            raise ValueError(f"unsupported workspace kind: {self.kind}")
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace directory does not exist: {root}")
        object.__setattr__(self, "root", root)


def git_root(cwd):
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        return None
    try:
        run = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=path, text=True,
            encoding="utf-8", errors="replace", capture_output=True, shell=False,
        )
    except OSError:
        return None
    if run.returncode:
        return None
    root = Path(run.stdout.strip()).expanduser().resolve()
    return root if root.is_dir() else None


def workspace_from_job(job):
    kind = job.get("workspace_kind") or "git"
    root = job.get("workspace_root") or job.get("project_root")
    return Workspace(kind, Path(root))


def _directory(value, kind="directory"):
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    return Workspace(kind, path) if path.is_dir() else None


def _parent_workspace(codex_home, parent_thread_id, parent_task_id=None):
    if not parent_thread_id:
        return None
    # Import lazily so state migration can use Workspace without a cycle.
    from .state import ACTIVE_STATES, load_job, runtime_home
    candidates = []
    for path in (runtime_home(codex_home) / "jobs").glob("*.json"):
        try:
            job = load_job(path)
        except (OSError, ValueError):
            continue
        if job.get("thread_id") != parent_thread_id:
            continue
        if parent_task_id is not None and str(job.get("task_id")) != str(parent_task_id):
            continue
        candidates.append(job)
    if parent_task_id is None:
        active = [job for job in candidates if job.get("status") in ACTIVE_STATES]
        candidates = active if active else candidates
    if len(candidates) != 1:
        return None
    return workspace_from_job(candidates[0])


def resolve_workspace(thread_id, explicit=None, actual_cwd=None, rollout_cwd=None,
                      codex_home=None, parent_thread_id=None, parent_task_id=None):
    """Resolve one workspace without scanning directory contents."""
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        root = git_root(path)
        return Workspace("git", root) if root is not None else Workspace("directory", path)

    for value in (actual_cwd, rollout_cwd):
        root = git_root(value)
        if root is not None:
            return Workspace("git", root)
    for value in (actual_cwd, rollout_cwd):
        workspace = _directory(value)
        if workspace is not None:
            return workspace

    inherited = _parent_workspace(codex_home, parent_thread_id, parent_task_id)
    if inherited is not None:
        return inherited

    configured_home = os.environ.get("CODEX_HOME")
    home = (Path(codex_home).expanduser().resolve() if codex_home else
            Path(configured_home).expanduser().resolve() if configured_home else
            (Path.home() / ".codex").resolve())
    root = home / "auto-resume" / "workspaces" / str(thread_id)
    root.mkdir(parents=True, exist_ok=True)
    return Workspace("managed", root)
