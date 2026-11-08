"""Tests for scripts/guard_bash.py. Run from the plugin root: python3 -m unittest discover -s tests"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "guard_bash.py"
sys.path.insert(0, str(HOOK.parent))

import guard_bash as g  # noqa: E402

REPO = "/work/repo"
PUBLISH = "/work/build-output"
SCRATCH = "/tmp/claude/scratchpad"
DEPLOY = "/usr/local/sbin/app-deploy"
SECRETS = ("/srv/app-secrets", "/var/lib/app-state")
PROTECTED = ("src/config/**", "deploy/**", "CLAUDE.md")


def context(agent_type=None, cwd=REPO, deploy=DEPLOY):
    return g.Context(
        repo=REPO, cwd=cwd, agent_type=agent_type, scratchpad=SCRATCH, publish_dir=PUBLISH,
        protected=PROTECTED, deploy_script=deploy, secret_paths=SECRETS,
    )


class Cases(unittest.TestCase):
    def blocked(self, command, **kw):
        self.assertIsNotNone(g.evaluate(command, context(**kw)), f"should be blocked: {command!r}")

    def allowed(self, command, **kw):
        self.assertIsNone(g.evaluate(command, context(**kw)), f"should be allowed: {command!r}")

    def asks(self, command, **kw):
        self.assertIsNone(g.evaluate(command, context(**kw)), f"should not be blocked: {command!r}")
        self.assertIsNotNone(g.evaluate_ask(command, context(**kw)), f"should ask: {command!r}")


class Sudo(Cases):
    def test_only_the_deployer_may_run_exactly_the_profiles_deploy_command(self):
        self.allowed(f"sudo {DEPLOY}", agent_type="deployer")
        self.allowed(f"sudo -n {DEPLOY}", agent_type="devteam:deployer")  # Plugin agents carry the plugin prefix.
        for agent in (None, "builder", "devteam:builder", "manager", "devteam:manager"):
            self.blocked(f"sudo {DEPLOY}", agent_type=agent)

    def test_nothing_else_even_for_the_deployer(self):
        for command in (
            f"sudo {DEPLOY} --flag", "sudo -i", "sudo bash", "sudo systemctl restart app", f"sudo {DEPLOY}; sudo id",
            "/usr/bin/sudo id", "bash -c 'sudo id'", "env sudo id", "echo x | sudo tee /etc/x", "su -", "doas id",
            'echo "$(sudo id)"', "eval 'sudo id'", "find . -exec sudo id \\;", "echo x | xargs sudo id",
        ):
            self.blocked(command, agent_type="deployer")

    def test_with_no_deploy_command_in_the_profile_there_is_no_sudo_at_all(self):
        self.blocked(f"sudo {DEPLOY}", agent_type="deployer", deploy=None)

    def test_the_word_sudo_in_text_is_fine(self):
        for command in ("echo sudo", 'git commit -m "sudo is blocked"', "grep -rn sudo README.md"):
            self.allowed(command)


class Quoting(Cases):
    def test_substitutions_inside_double_quotes_are_checked(self):
        self.blocked('echo "$(sudo id)"')
        self.blocked('echo "it\'s $(sudo id)"')
        self.blocked('X="`sudo id`"')

    def test_single_quotes_are_plain_text(self):
        self.allowed("echo '$(sudo id)'")

    def test_heredoc_bodies_are_data(self):
        self.allowed("cat <<'EOF'\nit's got 'quotes and sudo id\nEOF")
        self.allowed('git commit -m "$(cat <<\'EOF\'\nfix: it\'s fine (really)\nEOF\n)"')
        self.blocked("cat <<EOF\n$(sudo id)\nEOF")
        self.blocked("cat <<'EOF'\nbody\nEOF\nsudo id")

    def test_unparseable_commands_are_blocked_not_guessed_at(self):
        self.blocked("echo 'unterminated")
        self.blocked('echo "$(echo hi"')


class Delete(Cases):
    def test_inside_the_repo_build_output_and_scratchpad_is_fine(self):
        for command in ("rm file", "rm -rf bin obj", f"rm -rf {PUBLISH}", f"rm -rf {SCRATCH}/x", "git clean -fdx",
                        "find . -name '*.orig' -delete", "cd src && rm -rf obj"):
            self.allowed(command)

    def test_outside_is_blocked(self):
        for command in ("rm -rf /", "rm /etc/passwd", "rm -rf ~", "rm -rf $HOME", "rm -rf ../other", f"rm -rf {REPO}/../x",
                        "rm -rf /work/repo-evil", "cd / && rm x", "echo x | xargs rm", "find /tmp -delete",
                        "bash -c 'rm -rf /tmp/x'", 'echo "$(rm -rf /tmp/x)"'):
            self.blocked(command)

    def test_the_repo_root_and_history_are_not_removable(self):
        for command in ("rm -rf .", f"rm -rf {REPO}", "rm -rf .git", "rm -rf *"):
            self.blocked(command)


class Team(Cases):
    def test_shell_writes_to_dot_claude_are_blocked(self):
        for command in ("rm .claude/settings.json", "cp /tmp/x .claude/settings.json", "mv /tmp/x .claude/agents/a.md",
                        "sed -i s/a/b/ .claude/project-profile.md", "tee .claude/x", "touch .claude/x", "cd .claude && rm x",
                        "bash -c 'rm .claude/x'", "find .claude -delete"):
            self.blocked(command)

    def test_reading_and_running_things_there_is_fine(self):
        for command in ("cat .claude/settings.json", "grep -rn sudo .claude", "ls .claude/agents", "git add .claude"):
            self.allowed(command)

    def test_shell_writes_to_protected_paths_ask_the_boss(self):
        for command in ("sed -i s/a/b/ CLAUDE.md", "rm deploy/script", "cp /tmp/x src/config/a.cs", "rm -rf deploy",
                        "cd deploy && touch x"):
            self.asks(command)
        for command in ("cat deploy/script", "touch docs/x", "git add deploy"):
            self.assertIsNone(g.evaluate_ask(command, context()), command)


class Git(Cases):
    def test_destructive_pushes_are_blocked_in_every_spelling(self):
        for command in ("git push --force", "git push -f", "git push origin +main", "git push origin :branch", "git push -fu origin x",
                        "git push --delete origin x", "git push --mirror", "git -C . push -f", "bash -c 'git push -f'"):
            self.blocked(command)

    def test_ordinary_git_is_fine(self):
        for command in ("git push", "git push -u origin feature", "git status", "git commit -m 'force push is blocked'"):
            self.allowed(command)


class Secrets(Cases):
    def test_the_profiles_secret_locations_cannot_be_mentioned(self):
        for command in ("cat /srv/app-secrets/token", "ls /var/lib/app-state", "bash -c 'cat /srv/app-secrets/x'"):
            self.blocked(command)
        self.allowed("ls /srv/app-secrets-docs")  # A different path that merely starts the same.

    def test_no_secret_locations_in_the_profile_means_nothing_extra_is_blocked(self):
        ctx = g.Context(repo=REPO, cwd=REPO, agent_type=None, scratchpad=None)
        self.assertIsNone(g.evaluate("cat /srv/app-secrets/token", ctx))


class Profile(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="devteam-test-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "project-profile.md").write_text(
            "# Profile\n\nProse is ignored.\n\n```devteam-profile\n"
            "# a comment\ndeploy_command_path: /usr/local/sbin/app-deploy\nbuild_output_dir: /work/out\n"
            "secret_locations: /srv/app-secrets, /var/lib/app-state\nprotected_paths_file: .claude/protected-paths.txt\n```\n"
        )
        (self.repo / ".claude" / "protected-paths.txt").write_text("# c\n\ndeploy/**\nCLAUDE.md\n")

    def run_hook(self, command, agent_type=None, **extra):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo), "agent_type": agent_type, **extra}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.repo), "PATH": "/usr/bin"})

    def test_values_are_read_from_the_fenced_block(self):
        profile = g.read_profile(str(self.repo))

        self.assertEqual(profile["deploy_command_path"], "/usr/local/sbin/app-deploy")
        self.assertEqual(g.split_list(profile["secret_locations"]), ("/srv/app-secrets", "/var/lib/app-state"))

    def test_a_missing_profile_gives_the_strict_defaults(self):
        (self.repo / ".claude" / "project-profile.md").unlink()

        self.assertEqual(self.run_hook("sudo /usr/local/sbin/app-deploy", "deployer").returncode, 2)
        self.assertEqual(self.run_hook("git push --force").returncode, 2)
        self.assertEqual(self.run_hook("dotnet test").returncode, 0)

    def test_the_deploy_exception_and_the_secrets_come_from_the_profile(self):
        self.assertEqual(self.run_hook("sudo /usr/local/sbin/app-deploy", "devteam:deployer").returncode, 0)
        self.assertEqual(self.run_hook("sudo /usr/local/sbin/app-deploy", "devteam:builder").returncode, 2)
        self.assertEqual(self.run_hook("cat /srv/app-secrets/x").returncode, 2)

    def test_a_protected_write_asks_and_a_rules_write_is_blocked(self):
        ask = self.run_hook("sed -i s/a/b/ CLAUDE.md")
        self.assertEqual(ask.returncode, 0)
        self.assertEqual(json.loads(ask.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(self.run_hook("rm .claude/protected-paths.txt").returncode, 2)

    def test_the_build_output_dir_is_an_allowed_place_to_delete(self):
        self.assertEqual(self.run_hook("rm -rf /work/out/old").returncode, 0)

    def test_a_profile_cannot_point_the_protected_list_outside_the_project(self):
        text = (self.repo / ".claude" / "project-profile.md").read_text().replace(".claude/protected-paths.txt", "../../etc/passwd")
        (self.repo / ".claude" / "project-profile.md").write_text(text)

        self.assertEqual(g.load_protected(str(self.repo), g.read_profile(str(self.repo))), ())

    def test_garbage_input_fails_closed(self):
        for raw in ("not json", ""):
            result = subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_other_tools_are_not_its_business(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/srv/app-secrets/x"}, "cwd": str(self.repo)}
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
