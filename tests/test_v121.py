import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skill" / "codex-auto-resume" / "scripts"
sys.path.insert(0, str(SCRIPTS))


class V121Tests(unittest.TestCase):
    def test_unified_python_entrypoint_exposes_required_commands(self):
        entry = SCRIPTS / "auto_resume.py"
        self.assertTrue(entry.is_file())
        result = subprocess.run(
            [sys.executable, str(entry), "--help"], text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True,
        )
        for command in ("preflight", "run-job", "daemon", "probe-limits", "status"):
            self.assertIn(command, result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            status = subprocess.run(
                [sys.executable, str(entry), "status", "--codex-home", tmp],
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=True,
            )
            self.assertFalse(json.loads(status.stdout)["running"])
            preflight = subprocess.run(
                [sys.executable, str(entry), "preflight", "--opt-out"],
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=True,
            )
            self.assertEqual("SKIPPED", json.loads(preflight.stdout)["outcome"])
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertIn("skill/codex-auto-resume/scripts/*.py", package["files"])

    def test_runtime_layout_migrates_legacy_daemon_files(self):
        state = importlib.import_module("auto_resume.state")
        daemon = importlib.import_module("auto_resume.daemon")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            runtime = home / "auto-resume"
            runtime.mkdir(parents=True)
            legacy = {
                "daemon-state.json": b"{}\n",
                "daemon.lock": b"123\n",
                "daemon.stdout.log": b"out\n",
                "daemon.stderr.log": b"err\n",
            }
            for name, raw in legacy.items():
                (runtime / name).write_bytes(raw)
            layout = state.ensure_runtime_layout(home)
            for name in ("jobs", "checkpoints", "logs", "state"):
                self.assertTrue(layout[name].is_dir(), name)
            self.assertEqual(b"{}\n", (layout["state"] / "daemon-state.json").read_bytes())
            self.assertEqual(b"123\n", (layout["state"] / "daemon.lock").read_bytes())
            self.assertEqual(b"out\n", (layout["logs"] / "daemon.stdout.log").read_bytes())
            self.assertEqual(b"err\n", (layout["logs"] / "daemon.stderr.log").read_bytes())
            self.assertEqual(layout["state"] / "daemon-state.json", daemon.daemon_state_path(home))

    def test_health_diagnostics_distinguish_warning_and_error_with_timeouts(self):
        core = importlib.import_module("installer.codex_auto_resume_installer.core")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            core._prepare_runtime_layout(home)
            calls = []

            def healthy(argv, **kwargs):
                calls.append((list(map(str, argv)), kwargs.get("timeout")))
                joined = " ".join(map(str, argv))
                if "login status" in joined:
                    return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT\n", "")
                if "probe-limits" in argv:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"limit_id": "codex"}), "")
                if " status " in f" {joined} ":
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "running": True, "pid": 42, "heartbeat_at": time.time(),
                    }), "")
                if "loginctl" in joined:
                    return subprocess.CompletedProcess(argv, 0, "no\n", "")
                raise AssertionError(argv)

            report = core._health_diagnostics(home, "linux", simulate=False, timeout=7,
                                              runner=healthy)
            self.assertEqual("ok", report["codex_login"]["status"])
            self.assertEqual("ok", report["rate_limit_probe"]["status"])
            self.assertEqual("ok", report["daemon_heartbeat"]["status"])
            self.assertEqual("ok", report["runtime_writable"]["status"])
            self.assertEqual("ok", report["linux_linger"]["status"])
            self.assertTrue(all(timeout == 7 for _argv, timeout in calls))
            self.assertFalse(any("enable-linger" in " ".join(argv) for argv, _ in calls))

            def unhealthy(argv, **kwargs):
                joined = " ".join(map(str, argv))
                if "probe-limits" in argv:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                if "login status" in joined:
                    return subprocess.CompletedProcess(argv, 1, "", "not logged in")
                if " status " in f" {joined} ":
                    return subprocess.CompletedProcess(argv, 0, "{}", "")
                if "loginctl" in joined:
                    return subprocess.CompletedProcess(argv, 1, "", "no session")
                raise AssertionError(argv)

            failed = core._health_diagnostics(home, "linux", simulate=False, timeout=3,
                                              runner=unhealthy)
            self.assertEqual("error", failed["codex_login"]["status"])
            self.assertEqual("error", failed["rate_limit_probe"]["status"])
            self.assertEqual("ok", failed["daemon_heartbeat"]["status"])
            self.assertIn("inactive", failed["daemon_heartbeat"]["detail"])
            self.assertEqual("ok", failed["linux_linger"]["status"])

    def test_runtime_writability_denial_returns_prompt_error(self):
        core = importlib.import_module("installer.codex_auto_resume_installer.core")
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            with mock.patch.object(core.os, "open", side_effect=PermissionError("denied")) as opened:
                report = core._health_diagnostics(
                    Path(tmp) / "codex", "win32", simulate=True, timeout=2)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1)
            self.assertEqual("error", report["runtime_writable"]["status"])
            self.assertIn("denied", report["runtime_writable"]["detail"])
            opened.assert_called_once()
            flags = opened.call_args.args[1]
            self.assertEqual(core.os.O_CREAT | core.os.O_EXCL | core.os.O_WRONLY, flags)

    def test_doctor_reports_healthy_and_degraded_states(self):
        core = importlib.import_module("installer.codex_auto_resume_installer.core")
        services = importlib.import_module("installer.codex_auto_resume_installer.services")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            core.install(ROOT, home, platform_name="linux", simulate=True,
                         skip_prerequisites=True)
            healthy = core.doctor(home, platform_name="linux", simulate=True,
                                  skip_prerequisites=True)
            self.assertTrue(healthy["ok"], healthy)
            self.assertFalse(healthy["degraded"], healthy)

            adapter = services.service_adapter(
                "linux", home, simulate=True, owned=True, backend="systemd user")

            def degraded_runner(argv, **_kwargs):
                joined = " ".join(map(str, argv))
                if "login status" in joined:
                    return subprocess.CompletedProcess(argv, 0, "Logged in\n", "")
                if "probe-limits" in argv:
                    return subprocess.CompletedProcess(argv, 0, '{"limit_id":"codex"}', "")
                if " status " in f" {joined} ":
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "running": True, "pid": 9, "heartbeat_at": time.time(),
                    }), "")
                if "loginctl" in joined:
                    return subprocess.CompletedProcess(argv, 0, "no\n", "")
                raise AssertionError(argv)

            with mock.patch.object(core, "service_adapter", return_value=adapter):
                degraded = core.doctor(
                    home, platform_name="linux", simulate=False, skip_prerequisites=True,
                    health_timeout=4, health_runner=degraded_runner,
                )
            self.assertTrue(degraded["ok"], degraded)
            self.assertFalse(degraded["degraded"], degraded)
            self.assertEqual([], degraded["warnings"])
            self.assertEqual([], degraded["errors"])

    def test_macos_first_install_creates_runtime_without_bootstrap(self):
        core = importlib.import_module("installer.codex_auto_resume_installer.core")
        services = importlib.import_module("installer.codex_auto_resume_installer.services")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "codex"

            calls = []

            def runner(argv, **_kwargs):
                calls.append(list(argv))
                if "print" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "not loaded")
                return subprocess.CompletedProcess(argv, 0, "", "")

            adapter = services.service_adapter(
                "darwin", home, user_home=base / "user", simulate=False, runner=runner,
            )
            with mock.patch.object(services.os, "getuid", return_value=501, create=True):
                result = core.install(ROOT, home, platform_name="darwin",
                                      skip_prerequisites=True, service=adapter)
            self.assertTrue((home / "auto-resume" / "logs").is_dir())
            self.assertTrue((home / "auto-resume" / "state").is_dir())
            self.assertEqual("on_demand", result["manifest"]["service"]["backend"])
            self.assertFalse(Path(result["manifest"]["service"]["config_path"]).exists())
            self.assertFalse(any("bootstrap" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
