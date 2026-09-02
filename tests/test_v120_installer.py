import importlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
BEGIN = "<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->"
END = "<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->"


def installer_api():
    return importlib.import_module("installer.codex_auto_resume_installer.core")


def services_api():
    return importlib.import_module("installer.codex_auto_resume_installer.services")


class V120InstallerTests(unittest.TestCase):
    def test_distribution_has_bilingual_docs_english_metadata_and_matching_versions(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual("1.5.3", package["version"])
        self.assertEqual("./bin/cli.mjs", package["bin"]["codex-auto-resume"])
        self.assertTrue((ROOT / "README.md").read_text(encoding="utf-8").startswith("# Codex Auto Resume\n"))
        self.assertTrue((ROOT / "README.zh-CN.md").read_text(encoding="utf-8").startswith("# Codex 自动续作\n"))
        metadata = (ROOT / "skill" / "codex-auto-resume" / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Codex Auto Resume"', metadata)
        self.assertIn("allow_implicit_invocation: true", metadata)
        self.assertEqual("1.5.3", (ROOT / "skill" / "codex-auto-resume" / "VERSION").read_text().strip())

    def test_simulated_install_is_transactional_idempotent_and_doctor_is_clean(self):
        api = installer_api()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir()
            original = b"\xef\xbb\xbfexisting\r\n"
            (home / "AGENTS.md").write_bytes(original)
            result = api.install(ROOT, home, platform_name="win32", simulate=True,
                                 skip_prerequisites=True)
            self.assertEqual("1.5.3", result["version"])
            manifest_path = home / "auto-resume" / "install-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("io.github.shangzhimingge.codex-auto-resume", manifest["product"])
            self.assertEqual("1.5.3", manifest["version"])
            self.assertEqual(original, Path(str(home / "AGENTS.md") + ".codex-auto-resume.backup").read_bytes())
            agents = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(1, agents.count(BEGIN))
            self.assertEqual(1, agents.count(END))
            first_manifest = manifest_path.read_bytes()
            api.install(ROOT, home, platform_name="win32", simulate=True,
                        skip_prerequisites=True)
            self.assertEqual(1, (home / "AGENTS.md").read_text(encoding="utf-8").count(BEGIN))
            self.assertEqual(first_manifest, manifest_path.read_bytes())
            report = api.doctor(home, platform_name="win32", simulate=True,
                                skip_prerequisites=True)
            self.assertTrue(report["ok"], report)

    def test_unknown_skill_is_not_adopted_or_overwritten(self):
        api = installer_api()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            destination = home / "skills" / "codex-auto-resume"
            destination.mkdir(parents=True)
            marker = destination / "user.txt"
            marker.write_text("private", encoding="utf-8")
            with self.assertRaises(api.OwnershipError):
                api.install(ROOT, home, platform_name="linux", simulate=True,
                            skip_prerequisites=True)
            self.assertEqual("private", marker.read_text(encoding="utf-8"))
            self.assertFalse((home / "auto-resume" / "install-manifest.json").exists())

    def test_modified_owned_skill_requires_explicit_adoption(self):
        api = installer_api()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            api.install(ROOT, home, platform_name="darwin", simulate=True,
                        skip_prerequisites=True)
            changed = home / "skills" / "codex-auto-resume" / "local.txt"
            changed.write_text("local", encoding="utf-8")
            with self.assertRaises(api.OwnershipError):
                api.install(ROOT, home, platform_name="darwin", simulate=True,
                            skip_prerequisites=True)
            api.install(ROOT, home, platform_name="darwin", simulate=True,
                        skip_prerequisites=True, adopt_existing=True)
            self.assertFalse(changed.exists())

    def test_failed_service_activation_rolls_back_every_managed_file(self):
        api = installer_api()
        destination = None
        restored_versions = []

        class FailingService:
            platform_name = "linux"

            def install(self, *args, **kwargs):
                raise RuntimeError("injected service failure")

            def snapshot(self):
                return None

            def restore(self, _snapshot):
                restored_versions.append((destination / "VERSION").read_text().strip())

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            destination = home / "skills" / "codex-auto-resume"
            shutil.copytree(ROOT / "skill" / "codex-auto-resume", destination)
            (destination / "VERSION").write_text("1.1.1\n", encoding="utf-8")
            before_skill = api.tree_digest(destination)
            agents = home / "AGENTS.md"
            before_agents = b"legacy\r\n"
            agents.write_bytes(before_agents)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                api.install(ROOT, home, platform_name="linux", simulate=True,
                            skip_prerequisites=True, service=FailingService(), adopt_existing=True)
            self.assertEqual(before_skill, api.tree_digest(destination))
            self.assertEqual(before_agents, agents.read_bytes())
            self.assertFalse((home / "auto-resume" / "install-manifest.json").exists())
            self.assertEqual(["1.1.1"], restored_versions)

    def test_uninstall_preserves_runtime_by_default_and_purge_is_explicit(self):
        api = installer_api()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            api.install(ROOT, home, platform_name="linux", simulate=True,
                        skip_prerequisites=True)
            job = home / "auto-resume" / "jobs" / "keep.json"
            job.parent.mkdir(parents=True, exist_ok=True)
            job.write_text("{}", encoding="utf-8")
            api.uninstall(home, platform_name="linux", simulate=True)
            self.assertTrue(job.exists())
            self.assertFalse((home / "skills" / "codex-auto-resume").exists())
            self.assertNotIn(BEGIN, (home / "AGENTS.md").read_text(encoding="utf-8"))
            api.install(ROOT, home, platform_name="linux", simulate=True,
                        skip_prerequisites=True)
            api.uninstall(home, platform_name="linux", simulate=True, purge_data=True)
            self.assertFalse((home / "auto-resume").exists())
            self.assertTrue(Path(str(home / "AGENTS.md") + ".codex-auto-resume.backup").exists())

    def test_service_adapters_remove_legacy_login_entries_and_report_on_demand(self):
        services = services_api()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            daemon = base / "daemon.py"
            daemon.write_text("pass\n", encoding="utf-8")
            for platform_name in ("win32", "darwin", "linux"):
                home = base / platform_name / "codex"
                user_home = base / platform_name / "user"
                adapter = services.service_adapter(
                    platform_name, home, user_home=user_home, simulate=True)
                adapter.config_path.parent.mkdir(parents=True, exist_ok=True)
                adapter.config_path.write_text("legacy\n", encoding="utf-8")
                if platform_name == "win32":
                    adapter.startup_path.parent.mkdir(parents=True, exist_ok=True)
                    adapter.startup_path.write_text("legacy\n", encoding="utf-8")
                metadata = adapter.install("python", daemon)
                self.assertEqual("on_demand", metadata["backend"])
                self.assertFalse(metadata["active"])
                self.assertIsNone(metadata["config_digest"])
                self.assertFalse(adapter.config_path.exists())
                if platform_name == "win32":
                    self.assertFalse(adapter.startup_path.exists())

    def test_only_known_legacy_digests_are_adopted_implicitly(self):
        api = installer_api()
        self.assertEqual(
            "d7cf8bc5e2cf304c6e80ef485b65ef34bb48788935d7435320dbfd50d980fc87",
            api.KNOWN_LEGACY_DIGESTS["1.1.1"],
        )
        self.assertEqual(
            "313850b87e523ec07c8366887db8b1fc57d553cef615029a2742751e5eac1044",
            api.KNOWN_LEGACY_DIGESTS["1.2.1"],
        )

    def test_platform_cleanup_invokes_only_removal_commands(self):
        services = services_api()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(list(map(str, argv)))
                return subprocess.CompletedProcess(argv, 1, "", "not found")

            for platform_name in ("win32", "darwin", "linux"):
                adapter = services.service_adapter(
                    platform_name, base / platform_name / "codex",
                    user_home=base / platform_name / "user", simulate=False, runner=runner)
                metadata = adapter.install("python", base / "daemon.py")
                self.assertEqual("on_demand", metadata["backend"])
            flattened = "\n".join(" ".join(call) for call in calls)
            self.assertNotIn(" /Create ", f" {flattened} ")
            self.assertNotIn(" bootstrap ", f" {flattened} ")
            self.assertNotIn(" enable --now ", f" {flattened} ")
            self.assertIn("/Delete", flattened)
            self.assertIn("bootout", flattened)
            self.assertIn("disable --now", flattened)

    def test_manifest_is_on_demand_and_uninstall_reuses_legacy_cleanup(self):
        api, services = installer_api(), services_api()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "codex"
            user_home = base / "appdata"
            adapter = services.service_adapter(
                "win32", home, user_home=user_home, simulate=True)
            result = api.install(ROOT, home, platform_name="win32",
                                 skip_prerequisites=True, service=adapter)
            service_record = result["manifest"]["service"]
            self.assertEqual("on_demand", service_record["backend"])
            self.assertFalse(service_record["active"])
            self.assertIsNone(service_record["config_digest"])
            adapter.startup_path.parent.mkdir(parents=True, exist_ok=True)
            adapter.startup_path.write_text("legacy\n", encoding="utf-8")
            with mock.patch.object(api, "service_adapter", return_value=adapter):
                removed = api.uninstall(home, platform_name="win32", simulate=True)
            self.assertTrue(removed["uninstalled"])
            self.assertFalse(adapter.startup_path.exists())


if __name__ == "__main__":
    unittest.main()
