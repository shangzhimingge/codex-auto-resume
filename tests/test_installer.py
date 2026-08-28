import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
BEGIN = "<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->"
END = "<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->"


class InstallerTests(unittest.TestCase):
    def run_installer(self, home, *extra):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
             "-CodexHome", str(home), *extra], cwd=ROOT, text=True, encoding="utf-8",
            errors="replace", capture_output=True, shell=False,
        )

    def test_installer_is_idempotent_preserves_existing_agents_and_can_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex home"
            home.mkdir()
            original = "prefix\n<!-- BEGIN SOL-LUNA-HANDOFF MANAGED BLOCK -->\nkeep-sol\n<!-- END SOL-LUNA-HANDOFF MANAGED BLOCK -->\nsuffix\n"
            agents = home / "AGENTS.md"
            original_bytes = b"\xef\xbb\xbf" + original.replace("\n", "\r\n").encode("utf-8")
            agents.write_bytes(original_bytes)
            first = self.run_installer(home)
            self.assertEqual(0, first.returncode, first.stderr)
            installed = home / "skills" / "codex-auto-resume"
            self.assertTrue((installed / "VERSION").is_file())
            self.assertEqual("1.1.1", (installed / "VERSION").read_text(encoding="utf-8").strip())
            enabled = agents.read_text(encoding="utf-8")
            self.assertIn(original.strip(), enabled)
            self.assertEqual(1, enabled.count(BEGIN))
            self.assertEqual(1, enabled.count(END))
            backup = Path(str(agents) + ".codex-auto-resume.backup")
            self.assertTrue(backup.is_file())
            self.assertEqual(original_bytes, backup.read_bytes())
            second = self.run_installer(home)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(enabled, agents.read_text(encoding="utf-8"))
            disabled = self.run_installer(home, "-DisableDefaultActivation")
            self.assertEqual(0, disabled.returncode, disabled.stderr)
            final = agents.read_text(encoding="utf-8")
            self.assertNotIn(BEGIN, final)
            self.assertNotIn(END, final)
            self.assertIn(original.strip(), final)
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertEqual(original_bytes, backup.read_bytes())
            self.assertEqual([backup], list(home.glob("AGENTS.md.codex-auto-resume.backup*")))

    def test_skill_allows_implicit_discovery_and_activation_has_opt_out(self):
        yaml = (ROOT / "skill" / "codex-auto-resume" / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: true", yaml)
        block = (ROOT / "activation" / "AGENTS.block.md").read_text(encoding="utf-8")
        self.assertIn(BEGIN, block)
        self.assertIn("AUTO_RESUME=OFF", block)
        self.assertIn("SKIPPED", block)

    def test_runtime_and_distribution_versions_are_1_1_1(self):
        skill = ROOT / "skill" / "codex-auto-resume"
        self.assertEqual("1.1.1", (skill / "VERSION").read_text(encoding="utf-8").strip())
        namespace = {}
        exec((skill / "scripts" / "auto_resume" / "__init__.py").read_text(encoding="utf-8"), namespace)
        self.assertEqual("1.1.1", namespace["__version__"])


if __name__ == "__main__":
    unittest.main()
