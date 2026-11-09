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
FAKE_TOKEN = "123456789" + ":" + "A" * 35
SECRETS = ("/srv/app-secrets", "/var/lib/app-state")
PROTECTED = ("src/config/**", "deploy/**", "CLAUDE.md")


def context(agent_type=None, cwd=REPO):
    return g.Context(
        repo=REPO, cwd=cwd, agent_type=agent_type, scratchpad=SCRATCH, publish_dir=PUBLISH,
        protected=PROTECTED, secret_paths=SECRETS,
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
    def test_no_agent_may_run_sudo_not_even_the_deploy_script_and_not_the_deployer(self):
        for agent in (None, "builder", "devteam:builder", "manager", "devteam:manager", "deployer", "devteam:deployer"):
            self.blocked(f"sudo {DEPLOY}", agent_type=agent)
            self.blocked(f"sudo -n {DEPLOY}", agent_type=agent)

    def test_every_disguise_is_blocked_too(self):
        for command in (
            f"sudo {DEPLOY} --flag", "sudo -i", "sudo bash", "sudo systemctl restart app", f"sudo {DEPLOY}; sudo id",
            "/usr/bin/sudo id", "bash -c 'sudo id'", "env sudo id", "echo x | sudo tee /etc/x", "su -", "doas id",
            'echo "$(sudo id)"', "eval 'sudo id'", "find . -exec sudo id \\;", "echo x | xargs sudo id",
        ):
            self.blocked(command, agent_type="deployer")

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
        for command in ("git status", "git commit -m 'force push is blocked'", "git log --oneline", "git diff main...HEAD"):
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
            "# a comment\nbuild_output_dir: /work/out\n"
            "secret_locations: /srv/app-secrets, /var/lib/app-state\nprotected_paths_file: .claude/protected-paths.txt\n```\n"
        )
        (self.repo / ".claude" / "protected-paths.txt").write_text("# c\n\ndeploy/**\nCLAUDE.md\n")

    def run_hook(self, command, agent_type=None, **extra):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo), "agent_type": agent_type, **extra}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.repo), "PATH": "/usr/bin"})

    def test_values_are_read_from_the_fenced_block(self):
        profile = g.read_profile(str(self.repo))

        self.assertEqual(profile["build_output_dir"], "/work/out")
        self.assertEqual(g.split_list(profile["secret_locations"]), ("/srv/app-secrets", "/var/lib/app-state"))

    def test_a_missing_profile_gives_the_strict_defaults(self):
        (self.repo / ".claude" / "project-profile.md").unlink()

        self.assertEqual(self.run_hook("sudo /usr/local/sbin/app-deploy", "deployer").returncode, 2)
        self.assertEqual(self.run_hook("git push --force").returncode, 2)
        self.assertEqual(self.run_hook("dotnet test").returncode, 0)

    def test_sudo_is_blocked_for_the_deployer_too_and_the_secrets_come_from_the_profile(self):
        self.assertEqual(self.run_hook("sudo /usr/local/sbin/app-deploy", "devteam:deployer").returncode, 2)
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


class PrePushScan(unittest.TestCase):
    """A plain `git push` is allowed only if the whole-repo secret scan is clean (fixtures excepted by the allow list)."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="devteam-push-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        (self.repo / "app.txt").write_text("hello\n")
        git("add", "-A")

    def push(self, command="git push"):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo)}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.repo), "PATH": "/usr/bin"})

    def put(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_a_clean_repo_may_push(self):
        for command in ("git push", "git push -u origin feature", "git push origin main"):
            self.assertEqual(self.push(command).returncode, 0, command)

    def test_a_secret_in_any_file_blocks_the_push_and_is_not_echoed(self):
        self.put("docs/reports/r.md", f"the token was {FAKE_TOKEN}\n")

        result = self.push()

        self.assertEqual(result.returncode, 2)
        self.assertIn("chat-bot-token", result.stderr)
        self.assertNotIn(FAKE_TOKEN, result.stderr + result.stdout)

    def test_an_untracked_file_counts_too(self):
        self.put("new/untracked.txt", f"{FAKE_TOKEN}\n")
        self.assertEqual(self.push().returncode, 2)

    def test_the_allow_list_excuses_only_that_kind_in_only_those_files(self):
        self.put("tests/TokenTests.cs", f'const string Fake = "{FAKE_TOKEN}";\n')
        self.put(".claude/secret-scan-allow.txt", "# fixtures\ntests/*TokenTests.cs chat-bot-token\n")
        self.assertEqual(self.push().returncode, 0)

        self.put("tests/Other.cs", f'const string Fake = "{FAKE_TOKEN}";\n')  # Same kind, different file.
        self.assertEqual(self.push().returncode, 2)

    def test_an_allow_list_entry_for_a_different_kind_does_not_help(self):
        self.put("tests/TokenTests.cs", f"{FAKE_TOKEN}\n")
        self.put(".claude/secret-scan-allow.txt", "tests/*TokenTests.cs email-address\n")
        self.assertEqual(self.push().returncode, 2)

    def test_an_entry_with_no_kind_is_ignored_so_it_cannot_silence_a_whole_file(self):
        self.put("tests/TokenTests.cs", f"{FAKE_TOKEN}\n")
        self.put(".claude/secret-scan-allow.txt", "tests/*TokenTests.cs\n")
        self.assertEqual(self.push().returncode, 2)

    def test_a_repo_that_cannot_be_scanned_blocks_the_push(self):
        gone = Path(tempfile.mkdtemp(prefix="devteam-notrepo-"))
        self.addCleanup(shutil.rmtree, gone, True)
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(gone)}
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                                env={"CLAUDE_PROJECT_DIR": str(gone), "PATH": "/usr/bin"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not run", result.stderr)

    def test_destructive_pushes_are_still_refused_before_any_scan(self):
        self.assertEqual(self.push("git push --force").returncode, 2)

    def test_scanning_does_not_run_for_other_git_commands(self):
        self.put("docs/r.md", f"{FAKE_TOKEN}\n")
        for command in ("git status", "git commit -m x", "git log"):
            self.assertEqual(self.push(command).returncode, 0, command)


class PushAsksTheBoss(unittest.TestCase):
    """T-007c: a push that is allowed and has a clean scan still asks the boss in the permission prompt (gate)."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="devteam-ask-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        (self.repo / "app.txt").write_text("hello\n")
        git("add", "-A")

    def verdict(self, command, agent_type=None):
        ctx = g.Context(
            repo=str(self.repo), cwd=str(self.repo), agent_type=agent_type, scratchpad=SCRATCH, publish_dir=PUBLISH,
            protected=PROTECTED, secret_paths=SECRETS,
        )
        return g.evaluate_full(command, ctx)

    def run_hook(self, command, agent_type=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo), "agent_type": agent_type}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.repo), "PATH": "/usr/bin"})

    def test_plain_push_asks(self):
        for command in (
            "git push",
            "git push -u origin feature",
            "git -C . push",
            "env git push",
            'bash -c "git push"',
            "cd x && git push",
            "timeout 60 git push origin main",
            "echo $(git push)",
        ):
            verdict = self.verdict(command)
            self.assertIsNone(verdict.deny, command)
            self.assertIsNotNone(verdict.ask, f"a push must ask the boss: {command!r}")
            self.assertIn("push", verdict.ask, command)

    def test_the_ask_is_the_documented_pretooluse_decision_and_the_call_is_not_blocked(self):
        result = self.run_hook("git push")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("push", decision["permissionDecisionReason"])

    def test_force_push_still_denied(self):
        for command in ("git push --force", "git push -f", "git push origin +main", "git push --delete origin x", "git -C . push -f",
                        "bash -c 'git push --mirror'"):
            verdict = self.verdict(command)
            self.assertIsNotNone(verdict.deny, command)
            self.assertIsNone(verdict.ask, command)
        result = self.run_hook("git push --force")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_scan_finding_denies_before_ask(self):
        (self.repo / "r.md").write_text(f"{FAKE_TOKEN}\n")

        verdict = self.verdict("git push")
        result = self.run_hook("git push")

        self.assertIsNotNone(verdict.deny)
        self.assertIsNone(verdict.ask)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(FAKE_TOKEN, result.stderr)

    def test_other_git_never_asks(self):
        for command in ("git status", "git commit -m x", "git commit -m 'git push later'", "git log --oneline", "git diff main...HEAD",
                        "git branch -d old", "git fetch", "git pull", "echo git push", "git config alias.push x", "ls push"):
            verdict = self.verdict(command)
            self.assertIsNone(verdict.deny, command)
            self.assertIsNone(verdict.ask, command)
            result = self.run_hook(command)
            self.assertEqual((result.returncode, result.stdout), (0, ""), command)

    def test_push_ask_independent_of_agent_type(self):
        for agent_type in (None, "builder", "manager", "devteam:builder", "deployer", "tester"):
            self.assertIsNotNone(self.verdict("git push", agent_type).ask, str(agent_type))
            decision = json.loads(self.run_hook("git push", agent_type).stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "ask", str(agent_type))

    def test_a_protected_write_and_a_push_in_one_command_still_ask(self):
        verdict = self.verdict("sed -i s/a/b/ CLAUDE.md && git push")

        self.assertIsNone(verdict.deny)
        self.assertIsNotNone(verdict.ask)


if __name__ == "__main__":
    unittest.main()
