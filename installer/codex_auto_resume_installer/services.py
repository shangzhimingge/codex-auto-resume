import hashlib
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
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


def _quote_cmd(value):
    value = str(value)
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


class BaseService:
    manager = "unknown"
    service_id = "unknown"

    def __init__(self, codex_home, user_home=None, simulate=False, owned=False, runner=None,
                 backend=None, command_timeout=15):
        self.codex_home = Path(codex_home).resolve()
        self.user_home = Path(user_home or Path.home()).resolve()
        self.simulate = bool(simulate)
        self.owned = bool(owned)
        self.runner = runner or subprocess.run
        self.backend = backend or self.manager
        self.command_timeout = command_timeout

    @property
    def config_path(self):
        raise NotImplementedError

    def render(self, python_executable, daemon_script):
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

    def snapshot(self):
        return {
            "config_exists": self.config_path.is_file(),
            "config_bytes": self.config_path.read_bytes() if self.config_path.is_file() else None,
            "active": self.status().get("active", False),
        }

    def registration_exists(self):
        return self.config_path.is_file()

    def install(self, python_executable, daemon_script):
        if not self.owned and self.registration_exists():
            raise ServiceOwnershipError(
                f"service already exists without ownership: {self.service_id}")
        self.python_executable = str(python_executable)
        self.daemon_script = str(daemon_script)
        atomic_write(self.config_path, self.render(python_executable, daemon_script))
        if not self.simulate:
            self._activate()
        return self.metadata(active=True)

    def uninstall(self):
        if not self.simulate:
            self._deactivate()
        try:
            self.config_path.unlink()
        except FileNotFoundError:
            pass

    def restore(self, snapshot):
        if snapshot is None:
            return
        if snapshot["config_exists"]:
            atomic_write(self.config_path, snapshot["config_bytes"])
        else:
            try:
                self.config_path.unlink()
            except FileNotFoundError:
                pass
        if self.simulate:
            return
        if snapshot["active"] and snapshot["config_exists"]:
            if self.owned:
                self._deactivate(ignore_missing=True)
            if self.owned or not self._exists():
                previous_owned = self.owned
                try:
                    self.owned = True
                    self._activate()
                finally:
                    self.owned = previous_owned
        elif not snapshot["active"]:
            self._deactivate(ignore_missing=True)

    def metadata(self, active):
        return {
            "platform": self.platform_name,
            "manager": self.manager,
            "id": self.service_id,
            "config_path": str(self.config_path),
            "config_digest": file_digest(self.config_path),
            "active": bool(active),
            "simulated": self.simulate,
            "backend": self.backend,
        }


class WindowsTaskService(BaseService):
    platform_name = "win32"
    manager = "schtasks"
    service_id = WINDOWS_TASK

    @property
    def config_path(self):
        return self.codex_home / "auto-resume" / "service" / "windows" / "codex-auto-resume.cmd"

    @property
    def startup_path(self):
        return (self.user_home / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                "Startup" / "CodexAutoResume.cmd")

    def render(self, python_executable, daemon_script):
        command = " ".join(_quote_cmd(value) for value in (
            python_executable, daemon_script, "run", "--codex-home", self.codex_home,
        ))
        return f"@echo off\r\nrem Managed by schtasks task {WINDOWS_TASK}\r\n{command}\r\n"

    def _task_exists(self):
        if self.simulate:
            return self.config_path.is_file() and self.backend == "scheduled_task"
        return self._run(["schtasks.exe", "/Query", "/TN", WINDOWS_TASK], check=False).returncode == 0

    def _exists(self):
        if self.backend == "startup":
            return self.startup_path.is_file()
        return self._task_exists()

    def registration_exists(self):
        return self.config_path.is_file() or self.startup_path.is_file() or self._task_exists()

    @staticmethod
    def _access_denied(result):
        detail = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
        return "access is denied" in detail or "access denied" in detail or "拒绝访问" in detail

    def _start_daemon(self, timeout=10):
        argv = [self.python_executable, self.daemon_script, "run", "--codex-home",
                str(self.codex_home)]
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                 getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, shell=False, creationflags=flags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._run([
                self.python_executable, self.daemon_script, "status", "--codex-home",
                str(self.codex_home),
            ], check=False)
            try:
                state = json.loads(status.stdout) if status.returncode == 0 else {}
            except (TypeError, ValueError):
                state = {}
            if state.get("running") and state.get("pid") == process.pid:
                if hasattr(process, "_handle"):
                    process._handle.Close()
                    process._child_created = False
                return process.pid
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            self._run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], check=False)
        raise ServiceError("startup daemon did not complete its verified startup handshake")

    def _stop_daemon(self):
        daemon = self.codex_home / "skills" / "codex-auto-resume" / "scripts" / "daemon.py"
        self._run([sys.executable, daemon, "stop", "--codex-home", self.codex_home])

    def _activate(self):
        if self.backend == "startup":
            if self.owned:
                self._stop_daemon()
            atomic_write(self.startup_path, self.config_path.read_bytes())
            self._start_daemon()
            return
        self.backend = "scheduled_task"
        if self._task_exists() and not self.owned:
            raise ServiceOwnershipError(f"scheduled task already exists without ownership: {WINDOWS_TASK}")
        if self._task_exists():
            self._run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK], check=False)
        created = self._run([
            "schtasks.exe", "/Create", "/TN", WINDOWS_TASK, "/TR", str(self.config_path),
            "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
        ], check=False)
        if created.returncode != 0:
            if not self._access_denied(created):
                detail = (created.stderr or created.stdout or "service command failed").strip()
                raise ServiceError(f"schtasks.exe exited {created.returncode}: {detail}")
            if self.startup_path.exists() and not self.owned:
                raise ServiceOwnershipError(
                    f"startup launcher already exists without ownership: {self.startup_path}")
            self.backend = "startup"
            atomic_write(self.startup_path, self.config_path.read_bytes())
            self._start_daemon()
            return
        self._run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK])

    def _deactivate(self, ignore_missing=False):
        if self.backend == "startup":
            self._stop_daemon()
            try:
                self.startup_path.unlink()
            except FileNotFoundError:
                pass
            return
        self._run(["schtasks.exe", "/End", "/TN", WINDOWS_TASK], check=False)
        result = self._run(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=False)
        if result.returncode != 0 and not ignore_missing and self._exists():
            raise ServiceError(f"failed to remove scheduled task: {WINDOWS_TASK}")

    def status(self):
        active = self._exists()
        return {**self.metadata(active), "active": active}

    def metadata(self, active):
        value = super().metadata(active)
        value.update({
            "backend": self.backend,
            "autostart_path": str(self.startup_path) if self.backend == "startup" else None,
            "autostart_digest": file_digest(self.startup_path) if self.backend == "startup" else None,
        })
        return value

    def snapshot(self):
        value = super().snapshot()
        value.update({
            "backend": self.backend,
            "task_exists": self._task_exists(),
            "startup_exists": self.startup_path.is_file(),
            "startup_bytes": self.startup_path.read_bytes() if self.startup_path.is_file() else None,
        })
        return value

    def restore(self, snapshot):
        if snapshot is None:
            return
        if self.backend == "startup" and not snapshot["startup_exists"]:
            self._stop_daemon()
        if snapshot["startup_exists"]:
            atomic_write(self.startup_path, snapshot["startup_bytes"])
        else:
            try:
                self.startup_path.unlink()
            except FileNotFoundError:
                pass
        current_task = self._task_exists()
        if current_task and not snapshot["task_exists"]:
            self._run(["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=False)
        self.backend = snapshot["backend"]
        if snapshot["config_exists"]:
            atomic_write(self.config_path, snapshot["config_bytes"])
        else:
            try:
                self.config_path.unlink()
            except FileNotFoundError:
                pass
        if snapshot["task_exists"] and not self._task_exists() and self.owned:
            self.backend = "scheduled_task"
            self._activate()
        elif snapshot["backend"] == "startup" and snapshot["active"]:
            self.backend = "startup"
            self._start_daemon()


class MacLaunchAgentService(BaseService):
    platform_name = "darwin"
    manager = "launchd LaunchAgents"
    service_id = MAC_LABEL

    @property
    def config_path(self):
        return self.user_home / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"

    @property
    def domain(self):
        return f"gui/{os.getuid()}"

    def render(self, python_executable, daemon_script):
        value = {
            "Label": MAC_LABEL,
            "ProgramArguments": [str(python_executable), str(daemon_script), "run",
                                 "--codex-home", str(self.codex_home)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardOutPath": str(self.codex_home / "auto-resume" / "logs" / "daemon.stdout.log"),
            "StandardErrorPath": str(self.codex_home / "auto-resume" / "logs" / "daemon.stderr.log"),
        }
        return plistlib.dumps(value, fmt=plistlib.FMT_XML)

    def _exists(self):
        if self.simulate:
            return self.config_path.is_file()
        result = self._run(["launchctl", "print", f"{self.domain}/{MAC_LABEL}"], check=False)
        return result.returncode == 0

    def registration_exists(self):
        return self.config_path.is_file() or self._exists()

    def _activate(self):
        if self._exists():
            if not self.owned:
                raise ServiceOwnershipError(f"launch agent already exists without ownership: {MAC_LABEL}")
            self._deactivate(ignore_missing=True)
        self._run(["launchctl", "bootstrap", self.domain, str(self.config_path)])

    def _deactivate(self, ignore_missing=False):
        result = self._run(["launchctl", "bootout", self.domain, str(self.config_path)], check=False)
        if result.returncode != 0 and not ignore_missing and self._exists():
            raise ServiceError(f"failed to unload launch agent: {MAC_LABEL}")

    def status(self):
        active = self.config_path.is_file() if self.simulate else self._exists()
        return {**self.metadata(active), "active": active}


class LinuxSystemdService(BaseService):
    platform_name = "linux"
    manager = "systemd user"
    service_id = LINUX_UNIT

    @property
    def config_path(self):
        return self.user_home / ".config" / "systemd" / "user" / LINUX_UNIT

    def render(self, python_executable, daemon_script):
        command = " ".join(shlex.quote(str(value)) for value in (
            python_executable, daemon_script, "run", "--codex-home", self.codex_home,
        ))
        return (
            "[Unit]\nDescription=Codex Auto Resume daemon\nAfter=default.target\n\n"
            f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    def _exists(self):
        if self.simulate:
            return self.config_path.is_file()
        enabled = self._run(["systemctl", "--user", "is-enabled", LINUX_UNIT], check=False)
        active = self._run(["systemctl", "--user", "is-active", LINUX_UNIT], check=False)
        return enabled.returncode == 0 and active.returncode == 0

    def registration_exists(self):
        if self.config_path.is_file():
            return True
        if self.simulate:
            return False
        return self._run(["systemctl", "--user", "cat", LINUX_UNIT], check=False).returncode == 0

    def _activate(self):
        if self._exists() and not self.owned:
            raise ServiceOwnershipError(f"systemd user unit already exists without ownership: {LINUX_UNIT}")
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", "--now", LINUX_UNIT])
        self._run(["systemctl", "--user", "restart", LINUX_UNIT])

    def _deactivate(self, ignore_missing=False):
        result = self._run(["systemctl", "--user", "disable", "--now", LINUX_UNIT], check=False)
        self._run(["systemctl", "--user", "daemon-reload"], check=False)
        if result.returncode != 0 and not ignore_missing and self._exists():
            raise ServiceError(f"failed to disable systemd user unit: {LINUX_UNIT}")

    def status(self):
        active = self.config_path.is_file() if self.simulate else self._exists()
        return {**self.metadata(active), "active": active}


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
    if cls is WindowsTaskService and backend is None:
        backend = "scheduled_task"
    return cls(codex_home, user_home=user_home, simulate=simulate, owned=owned, runner=runner,
               backend=backend, command_timeout=command_timeout)
