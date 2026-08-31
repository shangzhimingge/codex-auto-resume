import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE_SKILL = ROOT / "skill" / "codex-auto-resume"


class SkillDocumentationTests(unittest.TestCase):
    def test_documented_registration_resolves_installed_skill_from_unrelated_project(self):
        text = (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8")
        root_lines = [
            '$CODEX_ROOT = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }',
            '$SKILL_ROOT = Split-Path -Parent (Resolve-Path (Join-Path $CODEX_ROOT "skills/codex-auto-resume/SKILL.md"))',
        ]
        for line in root_lines:
            self.assertIn(line, text)
        match = re.search(
            r'^python "\$SKILL_ROOT/scripts/register\.py" --thread-id "\$THREAD_ID" '
            r'--project "\$PROJECT" --goal "\$ORIGINAL_GOAL"$', text, re.MULTILINE)
        self.assertIsNotNone(match)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            codex_home = tmp / "codex home"
            installed = codex_home / "skills" / "codex-auto-resume"
            installed.parent.mkdir(parents=True)
            shutil.copytree(SOURCE_SKILL, installed)
            project = tmp / "unrelated target project"
            project.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            (project / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
            thread_id = str(uuid.uuid4())
            goal = "goal with several spaces"
            quote = lambda value: str(value).replace("'", "''")
            script = "\n".join([
                f"$env:CODEX_HOME = '{quote(codex_home)}'",
                f"$THREAD_ID = '{thread_id}'",
                f"$PROJECT = '{quote(project)}'",
                f"$ORIGINAL_GOAL = '{goal}'",
                *root_lines,
                match.group(0) + " --no-start",
            ])
            env = os.environ.copy()
            env.pop("CODEX_THREAD_ID", None)
            env["PYTHONUTF8"] = "1"
            powershell = (
                shutil.which("pwsh")
                or shutil.which("powershell.exe")
                or shutil.which("powershell")
            )
            if powershell is None:
                self.skipTest("PowerShell is not available on this runner")
            run = subprocess.run([powershell, "-NoProfile", "-Command", script],
                                 cwd=project, text=True, encoding="utf-8", errors="replace",
                                 capture_output=True, env=env, shell=False)
            self.assertEqual(0, run.returncode, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual(goal, payload["original_goal"])
            self.assertEqual(str(project.resolve()), payload["project_root"])
            self.assertTrue(Path(payload["checkpoint_path"]).is_file())

    def test_every_documented_script_invocation_uses_quoted_skill_root(self):
        text = (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8")
        invocations = re.findall(r'^python .+?\.py.*$', text, re.MULTILINE)
        self.assertGreaterEqual(len(invocations), 5)
        for invocation in invocations:
            self.assertRegex(invocation, r'^python "\$SKILL_ROOT/scripts/[^\"]+\.py"')
            self.assertNotRegex(invocation, r'--(?:project|goal) <')


if __name__ == "__main__":
    unittest.main()
