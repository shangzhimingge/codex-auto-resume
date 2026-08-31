import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .services import file_digest, service_adapter

PRODUCT = "io.github.shangzhimingge.codex-auto-resume"
VERSION = "1.3.0"
BEGIN = "<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->"
END = "<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->"
MANIFEST_SCHEMA = 1
KNOWN_LEGACY_DIGESTS = {
    "1.1.0": "d80744c81d3d6fd58c1b1ba0d2ec7194e7f7ae2c95fd75c44be5a7aa4156b27b",
    "1.1.1": "d7cf8bc5e2cf304c6e80ef485b65ef34bb48788935d7435320dbfd50d980fc87",
    "1.2.0": "f44ca8e992174d9c8f6c86473ba9d32d38fb96f936ba8d7242fefd8fc63ace2c",
    "1.2.1": "313850b87e523ec07c8366887db8b1fc57d553cef615029a2742751e5eac1044",
}


class InstallError(RuntimeError):
    pass


class OwnershipError(InstallError):
    pass


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_json(path, value):
    _atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise OwnershipError(f"invalid ownership manifest: {path}") from exc


def _assert_plain_tree(root):
    root = Path(root)
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise OwnershipError(f"symbolic-link roots are not managed: {root}")
    if not root.exists():
        return
    for value in root.rglob("*"):
        if value.is_symlink() or (hasattr(value, "is_junction") and value.is_junction()):
            raise OwnershipError(f"symbolic links are not managed: {value}")


def _assert_managed_path(home, target):
    home, target = Path(home).resolve(), Path(target)
    try:
        target.resolve(strict=False).relative_to(home)
    except ValueError as exc:
        raise OwnershipError(f"managed path escapes CODEX_HOME: {target}") from exc
    if target.exists() and (target.is_symlink() or
                            (hasattr(target, "is_junction") and target.is_junction())):
        raise OwnershipError(f"managed path is a link or junction: {target}")
    current = home
    for part in target.relative_to(home).parts[:-1]:
        current /= part
        if current.exists() and (current.is_symlink() or
                                 (hasattr(current, "is_junction") and current.is_junction())):
            raise OwnershipError(f"managed path traverses a link or junction: {current}")


def _included_file(path):
    return "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}


def tree_digest(root):
    root = Path(root)
    if not root.is_dir():
        return None
    _assert_plain_tree(root)
    digest = hashlib.sha256()
    for file in sorted((value for value in root.rglob("*") if value.is_file() and _included_file(value)),
                       key=lambda value: value.relative_to(root).as_posix()):
        relative = file.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = file.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _copy_skill(source, destination):
    _assert_plain_tree(source)
    shutil.copytree(
        source, destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        copy_function=shutil.copy2,
    )


def _legacy_skill_signature(path):
    path = Path(path)
    try:
        skill = (path / "SKILL.md").read_text(encoding="utf-8")
        version = (path / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    required = (
        path / "scripts" / "register.py",
        path / "scripts" / "watchdog.py",
        path / "scripts" / "auto_resume" / "__init__.py",
    )
    return (re.search(r"(?m)^name:\s*codex-auto-resume\s*$", skill) is not None and
            re.fullmatch(r"\d+\.\d+\.\d+", version) is not None and
            all(value.is_file() for value in required))


def _known_legacy_skill(path):
    if not _legacy_skill_signature(path):
        return False
    version = (Path(path) / "VERSION").read_text(encoding="utf-8").strip()
    return KNOWN_LEGACY_DIGESTS.get(version) == tree_digest(path)


def _read_utf8_agents(raw, path):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InstallError(f"AGENTS.md must be valid UTF-8: {path}") from exc


def _count(text, marker):
    return text.count(marker)


def remove_managed_block(existing):
    begins, ends = _count(existing, BEGIN), _count(existing, END)
    if begins == 0 and ends == 0:
        return existing
    if begins != 1 or ends != 1:
        raise OwnershipError("AGENTS.md has malformed or duplicate managed markers")
    start = existing.index(BEGIN)
    finish = existing.index(END, start) + len(END)
    if existing[finish:finish + 2] == "\r\n":
        finish += 2
    elif existing[finish:finish + 1] == "\n":
        finish += 1
    return existing[:start] + existing[finish:]


def compose_agents(existing, block, enabled):
    base = remove_managed_block(existing).rstrip("\r\n")
    if not enabled:
        return f"{base}\n" if base else ""
    normalized = block.replace("\r\n", "\n").strip()
    if _count(normalized, BEGIN) != 1 or _count(normalized, END) != 1:
        raise InstallError("activation block must contain one begin/end marker pair")
    if not normalized.startswith(BEGIN) or not normalized.endswith(END):
        raise InstallError("activation block marker boundaries are malformed")
    return f"{base}\n\n{normalized}\n" if base else f"{normalized}\n"


def _manifest_paths(codex_home):
    home = Path(codex_home).expanduser().resolve()
    return {
        "home": home,
        "skill": home / "skills" / "codex-auto-resume",
        "agents": home / "AGENTS.md",
        "backup": Path(str(home / "AGENTS.md") + ".codex-auto-resume.backup"),
        "runtime": home / "auto-resume",
        "manifest": home / "auto-resume" / "install-manifest.json",
    }


def _validate_manifest(manifest, paths):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise OwnershipError("unsupported ownership manifest schema")
    required = {
        "product", "version", "codex_home", "skill_path", "skill_digest",
        "agents_path", "agents_backup_path", "agents_backup_digest",
        "activation_enabled", "service", "adopted_legacy", "installed_at",
    }
    if not required <= set(manifest):
        raise OwnershipError("ownership manifest is incomplete")
    if manifest.get("product") != PRODUCT:
        raise OwnershipError("ownership manifest belongs to another product")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("skill_digest", ""))):
        raise OwnershipError("ownership manifest Skill digest is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("agents_backup_digest", ""))):
        raise OwnershipError("ownership manifest backup digest is malformed")
    if not isinstance(manifest.get("activation_enabled"), bool):
        raise OwnershipError("ownership manifest activation state is malformed")
    if Path(manifest.get("codex_home", "")).resolve() != paths["home"]:
        raise OwnershipError("ownership manifest CODEX_HOME mismatch")
    if Path(manifest.get("skill_path", "")).resolve() != paths["skill"]:
        raise OwnershipError("ownership manifest skill path mismatch")
    if Path(manifest.get("agents_path", "")).resolve() != paths["agents"]:
        raise OwnershipError("ownership manifest AGENTS.md path mismatch")
    if Path(manifest.get("agents_backup_path", "")).resolve() != paths["backup"]:
        raise OwnershipError("ownership manifest backup path mismatch")
    service = manifest.get("service")
    if not isinstance(service, dict) or not {
            "platform", "manager", "id", "config_path", "config_digest", "active", "simulated",
            "backend",
    } <= set(service):
        raise OwnershipError("ownership manifest service record is malformed")
    if service.get("platform") not in {"win32", "darwin", "linux"}:
        raise OwnershipError("ownership manifest service platform is unsupported")
    if not re.fullmatch(r"[0-9a-f]{64}", str(service.get("config_digest", ""))):
        raise OwnershipError("ownership manifest service digest is malformed")
    if service.get("backend") not in {"scheduled_task", "startup", "launchd LaunchAgents",
                                      "systemd user"}:
        raise OwnershipError("ownership manifest service backend is unsupported")
    if service.get("backend") == "startup":
        if not service.get("autostart_path") or not re.fullmatch(
                r"[0-9a-f]{64}", str(service.get("autostart_digest", ""))):
            raise OwnershipError("ownership manifest startup record is malformed")
    return manifest


def _restore_file(path, existed, raw):
    path = Path(path)
    if existed:
        _atomic_write(path, raw)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _prerequisites():
    return {
        "python": sys.version_info >= (3, 9),
        "git": shutil.which("git") is not None,
        "codex": shutil.which("codex") is not None,
    }


def _prepare_runtime_layout(codex_home):
    root = Path(codex_home).expanduser().resolve() / "auto-resume"
    layout = {
        "root": root,
        "jobs": root / "jobs",
        "checkpoints": root / "checkpoints",
        "logs": root / "logs",
        "state": root / "state",
    }
    for name in ("jobs", "checkpoints", "logs", "state"):
        layout[name].mkdir(parents=True, exist_ok=True)
    migrations = {
        root / "daemon-state.json": layout["state"] / "daemon-state.json",
        root / "daemon.lock": layout["state"] / "daemon.lock",
        root / "daemon.stdout.log": layout["logs"] / "daemon.stdout.log",
        root / "daemon.stderr.log": layout["logs"] / "daemon.stderr.log",
    }
    for legacy, destination in migrations.items():
        if legacy.exists() and not destination.exists():
            try:
                os.replace(legacy, destination)
            except OSError:
                # A v1.2.0 daemon may still own its root-level lock. The newly
                # activated daemon completes this migration after the old process stops.
                pass
    return layout


def _diagnostic(status, detail):
    return {"status": status, "detail": detail}


def _run_health(argv, timeout, runner):
    try:
        result = runner(
            list(map(str, argv)), text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, check=False,
            timeout=timeout,
        )
        return result, None
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} seconds"
    except OSError as exc:
        return None, str(exc)


def _health_diagnostics(codex_home, platform_name, simulate=False, timeout=15, runner=None):
    runner = runner or subprocess.run
    layout = _prepare_runtime_layout(codex_home)
    diagnostics = {}
    if simulate:
        diagnostics.update({
            "codex_login": _diagnostic("ok", "simulated"),
            "rate_limit_probe": _diagnostic("ok", "simulated"),
            "daemon_heartbeat": _diagnostic("ok", "simulated"),
        })
    else:
        codex = shutil.which("codex") or "codex"
        login, error = _run_health([codex, "login", "status"], timeout, runner)
        if error:
            diagnostics["codex_login"] = _diagnostic("error", error)
        elif login.returncode == 0:
            diagnostics["codex_login"] = _diagnostic("ok", (login.stdout or "logged in").strip())
        else:
            diagnostics["codex_login"] = _diagnostic(
                "error", (login.stderr or login.stdout or "Codex is not logged in").strip())

        unified = (Path(codex_home).expanduser().resolve() / "skills" / "codex-auto-resume" /
                   "scripts" / "auto_resume.py")
        probe, error = _run_health(
            [sys.executable, unified, "probe-limits", "--timeout", str(timeout)], timeout, runner)
        if error:
            diagnostics["rate_limit_probe"] = _diagnostic("error", error)
        elif probe.returncode != 0:
            diagnostics["rate_limit_probe"] = _diagnostic(
                "error", (probe.stderr or probe.stdout or "rate-limit probe failed").strip())
        else:
            try:
                snapshot = json.loads(probe.stdout)
                if not snapshot.get("limit_id"):
                    raise ValueError("missing limit_id")
                diagnostics["rate_limit_probe"] = _diagnostic("ok", snapshot["limit_id"])
            except (ValueError, AttributeError):
                diagnostics["rate_limit_probe"] = _diagnostic("error", "malformed probe response")

        daemon, error = _run_health(
            [sys.executable, unified, "status", "--codex-home", codex_home], timeout, runner)
        if error:
            diagnostics["daemon_heartbeat"] = _diagnostic("error", error)
        elif daemon.returncode != 0:
            diagnostics["daemon_heartbeat"] = _diagnostic(
                "error", (daemon.stderr or daemon.stdout or "daemon status failed").strip())
        else:
            try:
                state = json.loads(daemon.stdout)
                heartbeat = state.get("heartbeat_at")
                age = time.time() - heartbeat
                if (not state.get("running") or isinstance(heartbeat, bool) or
                        not isinstance(heartbeat, (int, float)) or age < -5 or age > 30):
                    raise ValueError("daemon lease or heartbeat is stale")
                diagnostics["daemon_heartbeat"] = _diagnostic(
                    "ok", f"pid={state.get('pid')} heartbeat_age={age:.1f}s")
            except (ValueError, TypeError, AttributeError):
                diagnostics["daemon_heartbeat"] = _diagnostic(
                    "error", "daemon lease or heartbeat is unavailable")

    write_probe = None
    fd = None
    try:
        fd, write_probe = tempfile.mkstemp(prefix=".doctor-", dir=layout["state"])
        os.write(fd, b"ok")
        os.close(fd)
        fd = None
        diagnostics["runtime_writable"] = _diagnostic("ok", str(layout["state"]))
    except OSError as exc:
        diagnostics["runtime_writable"] = _diagnostic("error", str(exc))
    finally:
        if fd is not None:
            os.close(fd)
        if write_probe:
            try:
                os.unlink(write_probe)
            except FileNotFoundError:
                pass

    if platform_name.lower() == "linux":
        if simulate:
            diagnostics["linux_linger"] = _diagnostic("ok", "simulated; installer never enables linger")
        else:
            linger, error = _run_health(
                ["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"],
                timeout, runner,
            )
            if error or linger.returncode != 0:
                detail = error or (linger.stderr or linger.stdout or "linger status unavailable").strip()
                diagnostics["linux_linger"] = _diagnostic(
                    "warning", f"{detail}; linger is never enabled automatically")
            elif linger.stdout.strip().lower() == "yes":
                diagnostics["linux_linger"] = _diagnostic("ok", "enabled")
            else:
                diagnostics["linux_linger"] = _diagnostic(
                    "warning", "disabled; background resume requires a user session")
    return diagnostics


def install(repo_root, codex_home, platform_name=None, simulate=False,
            skip_prerequisites=False, adopt_existing=False, service=None,
            disable_default_activation=False):
    repo_root = Path(repo_root).resolve()
    paths = _manifest_paths(codex_home)
    source = repo_root / "skill" / "codex-auto-resume"
    block_path = repo_root / "activation" / "AGENTS.block.md"
    if not source.is_dir() or not block_path.is_file():
        raise InstallError(f"distribution is incomplete: {repo_root}")
    if (source / "VERSION").read_text(encoding="utf-8").strip() != VERSION:
        raise InstallError("distribution version mismatch")
    requirements = _prerequisites()
    if not skip_prerequisites and not all(requirements.values()):
        missing = ", ".join(name for name, value in requirements.items() if not value)
        raise InstallError(f"missing prerequisites: {missing}")

    paths["home"].mkdir(parents=True, exist_ok=True)
    if paths["home"].is_symlink():
        raise OwnershipError(f"symbolic-link CODEX_HOME is not managed: {paths['home']}")
    for name in ("skill", "agents", "backup", "runtime", "manifest"):
        _assert_managed_path(paths["home"], paths[name])
    manifest = _validate_manifest(_load_json(paths["manifest"]), paths) if paths["manifest"].is_file() else None
    destination_exists = paths["skill"].exists()
    if destination_exists:
        _assert_plain_tree(paths["skill"])
        if manifest:
            actual = tree_digest(paths["skill"])
            if actual != manifest.get("skill_digest") and not adopt_existing:
                raise OwnershipError("installed Skill differs from the ownership manifest; use --adopt-existing")
            if actual != manifest.get("skill_digest") and not _legacy_skill_signature(paths["skill"]):
                raise OwnershipError("modified Skill does not match the codex-auto-resume signature")
        elif not _known_legacy_skill(paths["skill"]):
            if not adopt_existing or not _legacy_skill_signature(paths["skill"]):
                raise OwnershipError(
                    "existing Skill is not a known release; use --adopt-existing after reviewing it")

    platform_name = (platform_name or sys.platform).lower()
    owned_service = bool(manifest and manifest.get("service", {}).get("platform") in {
        platform_name, "win32" if platform_name == "windows" else platform_name,
    })
    _prepare_runtime_layout(paths["home"])
    adapter = service or service_adapter(
        platform_name, paths["home"], simulate=simulate, owned=owned_service,
        backend=manifest.get("service", {}).get("backend") if manifest else None,
    )
    activation_enabled = not disable_default_activation
    source_digest = tree_digest(source)
    agents_raw = paths["agents"].read_bytes() if paths["agents"].is_file() else b""
    agents_text = _read_utf8_agents(agents_raw, paths["agents"])
    block = block_path.read_text(encoding="utf-8")
    updated_agents = compose_agents(agents_text, block, activation_enabled)

    if manifest and tree_digest(paths["skill"]) == source_digest:
        same_activation = manifest.get("activation_enabled") is activation_enabled
        same_agents = updated_agents == agents_text
        service_status = adapter.status() if hasattr(adapter, "status") else {"active": False}
        backup_ok = paths["backup"].is_file()
        if same_activation and same_agents and service_status.get("active") and backup_ok:
            return {"version": VERSION, "manifest": manifest, "idempotent": True}

    skills_root = paths["skill"].parent
    skills_root.mkdir(parents=True, exist_ok=True)
    transaction = skills_root / f".codex-auto-resume.transaction-{uuid.uuid4().hex}"
    stage, previous = transaction / "stage", transaction / "previous"
    transaction.mkdir()
    _copy_skill(source, stage)
    service_snapshot = adapter.snapshot()
    snapshots = {
        "agents": (paths["agents"].exists(), agents_raw),
        "backup": (paths["backup"].exists(), paths["backup"].read_bytes() if paths["backup"].is_file() else b""),
        "manifest": (paths["manifest"].exists(), paths["manifest"].read_bytes() if paths["manifest"].is_file() else b""),
    }
    moved_previous = False
    try:
        if not paths["backup"].exists():
            paths["backup"].parent.mkdir(parents=True, exist_ok=True)
            with paths["backup"].open("xb") as handle:
                handle.write(agents_raw)
                handle.flush()
                os.fsync(handle.fileno())
        if paths["skill"].exists():
            os.replace(paths["skill"], previous)
            moved_previous = True
        os.replace(stage, paths["skill"])
        if updated_agents != agents_text:
            _atomic_write(paths["agents"], updated_agents)
        daemon_script = paths["skill"] / "scripts" / "daemon.py"
        service_metadata = adapter.install(sys.executable, daemon_script)
        installed_digest = tree_digest(paths["skill"])
        manifest_value = {
            "schema_version": MANIFEST_SCHEMA,
            "product": PRODUCT,
            "version": VERSION,
            "codex_home": str(paths["home"]),
            "skill_path": str(paths["skill"]),
            "skill_digest": installed_digest,
            "agents_path": str(paths["agents"]),
            "agents_backup_path": str(paths["backup"]),
            "agents_backup_digest": file_digest(paths["backup"]),
            "activation_enabled": activation_enabled,
            "service": service_metadata,
            "adopted_legacy": (manifest.get("adopted_legacy", False) if manifest
                               else destination_exists),
            "installed_at": int(time.time()),
        }
        _write_json(paths["manifest"], manifest_value)
    except Exception as exc:
        rollback_errors = []
        try:
            if paths["skill"].exists():
                shutil.rmtree(paths["skill"])
            if moved_previous and previous.exists():
                os.replace(previous, paths["skill"])
        except Exception as rollback_exc:
            rollback_errors.append(f"skill: {rollback_exc}")
        for name in ("agents", "backup", "manifest"):
            try:
                _restore_file(paths[name], *snapshots[name])
            except Exception as rollback_exc:
                rollback_errors.append(f"{name}: {rollback_exc}")
        try:
            adapter.restore(service_snapshot)
        except Exception as rollback_exc:
            rollback_errors.append(f"service: {rollback_exc}")
        detail = f"; rollback failures: {', '.join(rollback_errors)}" if rollback_errors else ""
        raise type(exc)(f"{exc}{detail}") from exc
    finally:
        shutil.rmtree(transaction, ignore_errors=True)
    return {"version": VERSION, "manifest": manifest_value, "idempotent": False}


def doctor(codex_home, platform_name=None, simulate=False, skip_prerequisites=False,
           health_timeout=15, health_runner=None):
    paths = _manifest_paths(codex_home)
    checks = {}
    requirements = _prerequisites()
    if not skip_prerequisites:
        checks.update({f"prerequisite_{key}": value for key, value in requirements.items()})
    try:
        manifest = _validate_manifest(_load_json(paths["manifest"]), paths)
        checks["manifest"] = True
    except OwnershipError:
        manifest = None
        checks["manifest"] = False
    if manifest:
        checks["skill_owned"] = tree_digest(paths["skill"]) == manifest.get("skill_digest")
        checks["backup"] = (file_digest(paths["backup"]) == manifest.get("agents_backup_digest"))
        try:
            agents = _read_utf8_agents(paths["agents"].read_bytes(), paths["agents"])
            markers = _count(agents, BEGIN) == 1 and _count(agents, END) == 1
            checks["activation"] = markers if manifest.get("activation_enabled") else not markers
        except OSError:
            checks["activation"] = not manifest.get("activation_enabled")
        adapter = service_adapter(
            platform_name or manifest["service"]["platform"], paths["home"],
            simulate=simulate, owned=True, backend=manifest["service"].get("backend"),
            command_timeout=health_timeout,
        )
        status = adapter.status()
        checks["service_identity"] = (manifest["service"]["platform"] == adapter.platform_name and
                                      manifest["service"]["id"] == adapter.service_id)
        configured = Path(manifest["service"]["config_path"]).resolve()
        checks["service_path"] = configured == adapter.config_path.resolve()
        checks["service_config"] = (checks["service_path"] and
                                    file_digest(configured) == manifest["service"].get("config_digest"))
        checks["service_backend"] = manifest["service"].get("backend") == adapter.backend
        if adapter.platform_name == "win32" and adapter.backend == "startup":
            autostart = Path(manifest["service"].get("autostart_path", "")).resolve()
            checks["service_autostart_path"] = autostart == adapter.startup_path.resolve()
            checks["service_autostart"] = (checks["service_autostart_path"] and
                                           file_digest(autostart) ==
                                           manifest["service"].get("autostart_digest"))
        checks["service_active"] = status.get("active", False)
    selected_platform = (platform_name or (manifest or {}).get("service", {}).get("platform") or
                         sys.platform)
    diagnostics = _health_diagnostics(
        paths["home"], selected_platform, simulate=simulate, timeout=health_timeout,
        runner=health_runner,
    )
    checks.update({name: value["status"] != "error" for name, value in diagnostics.items()})
    warnings = [name for name, value in diagnostics.items() if value["status"] == "warning"]
    errors = [name for name, value in diagnostics.items() if value["status"] == "error"]
    return {
        "ok": (all(checks.values()) if checks else False) and not errors,
        "degraded": bool(warnings),
        "checks": checks,
        "diagnostics": diagnostics,
        "warnings": warnings,
        "errors": errors,
        "manifest": manifest,
    }


def uninstall(codex_home, platform_name=None, simulate=False, purge_data=False):
    paths = _manifest_paths(codex_home)
    if not paths["manifest"].is_file():
        if not paths["skill"].exists():
            return {"uninstalled": False, "reason": "not_installed"}
        raise OwnershipError("ownership manifest is required before uninstall")
    manifest = _validate_manifest(_load_json(paths["manifest"]), paths)
    if tree_digest(paths["skill"]) != manifest.get("skill_digest"):
        raise OwnershipError("installed Skill was modified after installation")
    service_info = manifest.get("service", {})
    adapter = service_adapter(
        platform_name or service_info.get("platform", sys.platform), paths["home"],
        simulate=simulate, owned=True, backend=service_info.get("backend"),
    )
    if service_info.get("platform") != adapter.platform_name or service_info.get("id") != adapter.service_id:
        raise OwnershipError("service identity differs from the ownership manifest")
    configured = Path(service_info.get("config_path", "")).resolve()
    if configured != adapter.config_path.resolve():
        raise OwnershipError("service configuration path differs from the ownership manifest")
    if file_digest(configured) != service_info.get("config_digest"):
        raise OwnershipError("service configuration was modified after installation")
    if adapter.platform_name == "win32" and adapter.backend == "startup":
        autostart = Path(service_info.get("autostart_path", "")).resolve()
        if autostart != adapter.startup_path.resolve():
            raise OwnershipError("startup launcher path differs from the ownership manifest")
        if file_digest(autostart) != service_info.get("autostart_digest"):
            raise OwnershipError("startup launcher was modified after installation")
    agents_existed = paths["agents"].is_file()
    agents_raw = paths["agents"].read_bytes() if agents_existed else b""
    agents_text = _read_utf8_agents(agents_raw, paths["agents"])
    updated_agents = compose_agents(agents_text, "", False)
    tombstone = paths["skill"].parent / f".codex-auto-resume.uninstall-{uuid.uuid4().hex}"
    service_snapshot = adapter.snapshot()
    moved_skill = False
    try:
        adapter.uninstall()
        os.replace(paths["skill"], tombstone)
        moved_skill = True
        if updated_agents != agents_text:
            _atomic_write(paths["agents"], updated_agents)
        paths["manifest"].unlink()
    except Exception:
        if moved_skill and not paths["skill"].exists() and tombstone.exists():
            os.replace(tombstone, paths["skill"])
        _restore_file(paths["agents"], agents_existed, agents_raw)
        adapter.restore(service_snapshot)
        raise
    shutil.rmtree(tombstone, ignore_errors=True)
    if purge_data:
        shutil.rmtree(paths["runtime"], ignore_errors=False)
    return {"uninstalled": True, "purged": bool(purge_data), "backup": str(paths["backup"])}
