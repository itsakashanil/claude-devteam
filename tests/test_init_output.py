"""What /devteam:init hands a project: the hook + scanner pair must work on their own, wired the way settings say."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "123456789" + ":" + "A" * 35


class InitInstallsAWorkingHook(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="init-project-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        subprocess.run(["git", "-C", str(self.project), "init", "-q", "-b", "main"], check=True, capture_output=True)
        hooks = self.project / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        for name in ("guard_bash.py", "secret_scan.py"):  # Exactly what the init skill says to copy.
            shutil.copy(ROOT / "scripts" / name, hooks / name)
        (hooks / "guard_bash.py").chmod(0o755)
        (self.project / "a.txt").write_text("hello\n")

    def hook(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.project)}
        # -I: no script-directory import path, no user site, no environment: as isolated as a pinned hook can be.
        return subprocess.run([sys.executable, "-I", str(self.project / ".claude" / "hooks" / "guard_bash.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.project), "PATH": "/usr/bin"})

    def test_the_copied_pair_blocks_sudo_and_force_pushes_and_allows_ordinary_work(self):
        self.assertEqual(self.hook("sudo id").returncode, 2)
        self.assertEqual(self.hook("git push --force").returncode, 2)
        self.assertEqual(self.hook("dotnet test").returncode, 0)

    def test_the_copied_pair_scans_before_every_push(self):
        self.assertEqual(self.hook("git push").returncode, 0)

        (self.project / "report.md").write_text(f"token {FAKE_TOKEN}\n")
        result = self.hook("git push origin main")

        self.assertEqual(result.returncode, 2)
        self.assertIn("pre-push secret scan", result.stderr)
        self.assertNotIn(FAKE_TOKEN, result.stderr)

    def test_the_allow_list_next_to_the_project_profile_is_honoured_by_the_copy(self):
        (self.project / "tests").mkdir()
        (self.project / "tests" / "TokenTests.cs").write_text(f"{FAKE_TOKEN}\n")
        (self.project / ".claude" / "secret-scan-allow.txt").write_text("tests/*TokenTests.cs chat-bot-token\n")

        self.assertEqual(self.hook("git push").returncode, 0)

    def test_a_scanner_missing_next_to_the_hook_blocks_pushes_rather_than_skipping_the_scan(self):
        (self.project / ".claude" / "hooks" / "secret_scan.py").unlink()
        self.assertEqual(self.hook("git push").returncode, 2)


class InitTemplatesAreWired(unittest.TestCase):
    def test_the_init_skill_copies_both_files_together_and_prints_the_wiring(self):
        text = (ROOT / "skills" / "init" / "SKILL.md").read_text()
        for phrase in ("${CLAUDE_PLUGIN_ROOT}/scripts/guard_bash.py", "${CLAUDE_PLUGIN_ROOT}/scripts/secret_scan.py",
                       ".claude/hooks/", "side by side", "allowManagedHooksOnly", "onFailure: block"):
            self.assertIn(phrase, text, phrase)

    def test_the_settings_template_wires_the_project_hook(self):
        settings = json.loads((ROOT / "templates" / "settings.json").read_text())
        (entry,) = settings["hooks"]["PreToolUse"]
        (hook,) = entry["hooks"]
        self.assertEqual(entry["matcher"], "Bash")
        self.assertEqual(hook["command"], "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_bash.py")
        self.assertEqual(hook["onFailure"], "block")

    def test_the_managed_example_pins_the_same_hook(self):
        managed = json.loads((ROOT / "templates" / "managed-settings.example.json").read_text())
        self.assertTrue(managed["allowManagedHooksOnly"])
        hook = managed["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertTrue(hook["command"].endswith("/.claude/hooks/guard_bash.py"))
        self.assertTrue(hook["command"].startswith("/"))
        self.assertEqual(hook["onFailure"], "block")


if __name__ == "__main__":
    unittest.main()
