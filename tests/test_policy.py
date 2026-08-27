import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PolicyTests(unittest.TestCase):
    def test_runtime_contains_no_forbidden_resume_or_credit_paths(self):
        scripts = ROOT / "skill" / "codex-auto-resume" / "scripts"
        source = "\n".join(path.read_text(encoding="utf-8") for path in scripts.rglob("*.py"))
        for forbidden in ("resume --last", "codex queue", "rateLimitResetCredit/consume", "resetCredit"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
