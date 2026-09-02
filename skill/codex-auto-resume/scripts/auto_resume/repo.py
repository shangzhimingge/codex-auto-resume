import hashlib
import subprocess
from pathlib import Path

from .workspace import Workspace


class RepoError(RuntimeError):
    pass


def _git(project, *args):
    run = subprocess.run(["git", *args], cwd=project, text=True, encoding="utf-8",
                         errors="replace", capture_output=True, shell=False)
    if run.returncode:
        raise RepoError(run.stderr.strip() or "git command failed")
    return run.stdout


def validate_repo(project):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"project directory does not exist: {project}")
    if _git(project, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise ValueError(f"not a Git work tree: {project}")
    return project


def _changed_paths(project):
    raw = _git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = raw.split("\0")
    paths = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and i < len(entries):
            path = entries[i]
            i += 1
            paths.append(path)
    return raw, sorted(set(paths))


def _git_fingerprint(project):
    project = validate_repo(project)
    head = _git(project, "rev-parse", "HEAD").strip()
    porcelain, paths = _changed_paths(project)
    hashes = {}
    for relative in paths:
        path = project / relative
        if path.is_file():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            hashes[relative] = None
    return {"head": head, "porcelain": porcelain, "files_sha256": hashes}


def _directory_fingerprint(workspace):
    root = workspace.root
    stat = root.stat()
    return {
        "kind": workspace.kind,
        "root": str(root),
        "directory_identity": {"device": stat.st_dev, "inode": stat.st_ino},
    }


def fingerprint(project):
    workspace = project if isinstance(project, Workspace) else Workspace("git", Path(project))
    if workspace.kind == "git":
        return _git_fingerprint(workspace.root)
    return _directory_fingerprint(workspace)


def repo_matches(project, expected):
    return fingerprint(project) == expected
