import hashlib
import os
import subprocess
import sys
from pathlib import Path

WINDOWS_TASK = r"\CodexAutoResume"
MAC_LABEL = "io.github.shangzhimingge.codex-auto-resume"
LINUX_UNIT = "codex-auto-resume.service"


class ServiceError(RuntimeError):
    pass


class ServiceOwnershipError(ServiceError):
    pass


def file_digest(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class BaseService:
    """Compatibility adapter that removes legacy login registrations."""

    manager = "on demand"
    service_id = "on_demand"

    def __init__(self, codex_home, user_home=None, simulate=False, owned=False, runner=None,
                 backend=None, command_timeout=15):
        self.codex_home = Path(codex_home).resolve()
        self.user_home = Path(user_home or Path.home()).resolve()
        self.simulate = bool(simulate)
        self.owned = bool(owned)
        self.runner = runner or subprocess.run
        self.previous_backend = backend
        self.backend = "on_demand"
        self.command_timeout = command_timeout

    @property
    def config_path(self):
        raise NotImplementedError

    def _run(self, argv, check=True):
        result = self.runner(
            list(map(str, argv)), text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, check=False,
            timeout=self.command_timeout,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "service command failed").strip()
            raise ServiceError(f"{argv[0]} exited {result.returncode}: {detail}")
        return result

    def registration_exists(self):
        return self.config_path.is_file()

    def cleanup_legacy(self):
        raise NotImplementedError

    def install(self, _python_executable, _daemon_script):
        self.cleanup_legacy()
        return self.metadata(active=False)

    def uninstall(self):
        self.cleanup_legacy()

    def status(self):
        active = self.registration_exists()
        return {**self.metadata(active), "active": active}

    def metadata(self, active):
        return {
            "platform": self.platform_name,
            "manager": self.manager,
            "id": self.service_id,
            "config_path": str(self.config_path),
            "config_digest": file_digest(self.config_path),
            "active": bool(active),
            "simulated": self.simulate,
            "backend": "on_demand",
        }

    def snapshot(self):
        return {
            "config_exists": self.config_path.is_file(),
            "config_bytes": self.config_path.read_bytes() if self.config_path.is_file() else None,
            "active": self.registration_exists(),
            "backend": self.previous_backend,
        }

    def restore(self, snapshot):
        if snapshot is None:
            return
        self.cleanup_legacy()
        if snapshot["config_exists"]:
            atomic_write(self.config_path, snapshot["config_bytes"])
        self._restore_registration(snapshot)

    def _restore_registration(self, _snapshot):
        return


class WindowsTaskService(BaseService):
    platform_name = "win32"
    service_id = WINDOWS_TASK

    @property
    def config_path(self):
        return self.codex_home / "auto-resume" / "service" / "windows" / "codex-auto-resume.cmd"

    @property
    def startup_path(self):
        return (self.user_home / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                "Startup" / "CodexAutoResume.cmd")

    def _task_exists(self):
        if self.simulate:
            return False
        return self._run(["schtasks.exe", "/Query", "/TN", WINDOWS_TASK], check=False).returncode == 0

    def registration_exists(self):
        return self.config_path.is_file() or self.startup_path.is_file() or self._task_exists()

    def cleanup_legacy(self):
        if (self.startup_path.is_file() or self.previous_backend == "startup") and not self.simulate:
            daemon = (self.codex_home / "skills" / "codex-auto-resume" /
                      "scripts" / "daemon.py")
            if daemon.is_file():
                self._run([sys.executable, daemon, "stop", "--codex-home", self.codex_home],
                          check=False)
        if not self.simulate:
            self._run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK], check=False)
            self._run(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=False)
        for path in (self.startup_path, self.config_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def metadata(self, active):
        value = super().metadata(active)
        value.update({"legacy_startup_path": str(self.startup_path)})
        return value

    def snapshot(self):
        value = super().snapshot()
        value.update({
            "task_exists": self._task_exists(),
            "startup_exists": self.startup_path.is_file(),
            "startup_bytes": self.startup_path.read_bytes() if self.startup_path.is_file() else None,
        })
        return value

    def _restore_registration(self, snapshot):
        if snapshot.get("startup_exists"):
            atomic_write(self.startup_path, snapshot["startup_bytes"])
        if snapshot.get("task_exists") and not self.simulate and snapshot.get("config_exists"):
            self._run([
                "schtasks.exe", "/Create", "/TN", WINDOWS_TASK, "/TR", str(self.config_path),
                "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
            ])


class MacLaunchAgentService(BaseService):
    platform_name = "darwin"
    service_id = MAC_LABEL

    @property
    def config_path(self):
        return self.user_home / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"

    @property
    def domain(self):
        return f"gui/{getattr(os, 'getuid', lambda: 0)()}"

    def _registered(self):
        if self.simulate:
            return self.config_path.is_file()
        return self._run(["launchctl", "print", f"{self.domain}/{MAC_LABEL}"], check=False).returncode == 0

    def registration_exists(self):
        return self.config_path.is_file() or self._registered()

    def cleanup_legacy(self):
        if not self.simulate:
            self._run(["launchctl", "bootout", self.domain, str(self.config_path)], check=False)
        try:
            self.config_path.unlink()
        except FileNotFoundError:
            pass

    def _restore_registration(self, snapshot):
        if snapshot.get("active") and snapshot.get("config_exists") and not self.simulate:
            self._run(["launchctl", "bootstrap", self.domain, str(self.config_path)])


class LinuxSystemdService(BaseService):
    platform_name = "linux"
    service_id = LINUX_UNIT

    @property
    def config_path(self):
        return self.user_home / ".config" / "systemd" / "user" / LINUX_UNIT

    def _registered(self):
        if self.simulate:
            return self.config_path.is_file()
        return self._run(["systemctl", "--user", "cat", LINUX_UNIT], check=False).returncode == 0

    def registration_exists(self):
        return self.config_path.is_file() or self._registered()

    def cleanup_legacy(self):
        if not self.simulate:
            self._run(["systemctl", "--user", "disable", "--now", LINUX_UNIT], check=False)
        try:
            self.config_path.unlink()
        except FileNotFoundError:
            pass
        if not self.simulate:
            self._run(["systemctl", "--user", "daemon-reload"], check=False)

    def _restore_registration(self, snapshot):
        if snapshot.get("active") and snapshot.get("config_exists") and not self.simulate:
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(["systemctl", "--user", "enable", "--now", LINUX_UNIT])


def service_adapter(platform_name, codex_home, user_home=None, simulate=False,
                    owned=False, runner=None, backend=None, command_timeout=15):
    normalized = platform_name.lower()
    classes = {
        "win32": WindowsTaskService,
        "windows": WindowsTaskService,
        "darwin": MacLaunchAgentService,
        "macos": MacLaunchAgentService,
        "linux": LinuxSystemdService,
    }
    try:
        cls = classes[normalized]
    except KeyError as exc:
        raise ServiceError(f"unsupported platform: {platform_name}") from exc
    if simulate and user_home is None:
        user_home = Path(codex_home) / "auto-resume" / "simulated-home"
    elif cls is WindowsTaskService and user_home is None:
        user_home = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return cls(codex_home, user_home=user_home, simulate=simulate, owned=owned, runner=runner,
               backend=backend, command_timeout=command_timeout)
