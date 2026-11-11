"""Tests for scripts/guard_bash.py. Run from the plugin root: python3 -m unittest discover -s tests"""

import json
import os
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
        self.blocked("echo '$(sudo id)'")  # Round 2: a quoted ( ) is refused, was allowed.
        self.allowed("echo '`sudo id`'")

    def test_heredoc_bodies_are_data(self):
        self.allowed("cat <<'EOF'\nit's got 'quotes and sudo id\nEOF")
        # Round 2: the substitution is inside double quotes, so its ( ) are quoted and refused (was allowed).
        self.blocked('git commit -m "$(cat <<\'EOF\'\nfix: it\'s fine (really)\nEOF\n)"')
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


class WriteTargets(Cases):
    """T-013a: redirections and write targets the shell would expand (no files needed on disk)."""

    def quiet(self, command):
        self.allowed(command)
        self.assertIsNone(g.evaluate_ask(command, context()), f"should not ask: {command!r}")

    def test_redirections_into_the_team_rules_are_blocked(self):
        for command in (
            "echo x > .claude/x",
            "echo x >> .claude/x",
            "echo x >| .claude/x",
            "echo x &> .claude/x",
            "echo x &>> .claude/x",
            "echo x 2> .claude/x",
            "echo x 1<> .claude/x",
            "echo x >& .claude/x",
            "echo x>.claude/x",
            "> .claude/x echo hi",
            "exec 3> .claude/x",
            "cat <<EOF > .claude/x\nbody\nEOF",
            "{ echo x; } > .claude/x",
            "(echo x) > .claude/x",
            "(echo x)>.claude/x",
            "cd .claude && echo x > s",
            "bash -c 'echo x > .claude/x'",
            "echo $(echo x > .claude/x)",
            'echo "$(echo x > .claude/x)"',
            f"echo x > {REPO}/.claude/x",
            "echo x > /tmp/ok > .claude/x",
        ):
            self.blocked(command)

    def test_redirections_into_protected_code_ask(self):
        for command in (
            "echo x > CLAUDE.md",
            "echo x >> deploy/x",
            "echo x > src/config/x",
            "> CLAUDE.md echo hi",
            "(echo x) > deploy/y",
        ):
            self.asks(command)

    def test_ordinary_redirections_pass_silently(self):
        for command in (
            "dotnet test > out.txt",
            "dotnet test 2>&1 | tail -5",
            "echo hi > /dev/null 2>&1",
            "echo hi &>/dev/null",
            "echo hi >&2",
            "echo hi 1>&2",
            "ls 2>/dev/null",
            f"echo hi > {SCRATCH}/out.txt",
            "cat < .claude/settings.json",
            "sort < in.txt > out.txt",
            "cat <<< 'hello'",
            "echo hi > ~/notes.txt",
            "cat <<'EOF' > notes.txt\nhello\nEOF",
        ):
            self.quiet(command)
        for command in (  # Round 2: a quoted > is refused, these were allowed.
            "echo 'a > .claude/x'",
            'git commit -m "x > y"',
        ):
            self.blocked(command)

    def test_a_redirection_without_a_target_is_blocked(self):
        for command in ("echo x >", "echo x >>", "echo x > ; ls", "echo x > > f", "echo x >&", "cat <>", "echo x &>"):
            self.blocked(command)

    def test_write_targets_the_shell_would_expand_are_blocked(self):
        for command in (
            'cp x "$PWD/.claude/settings.json"',
            "touch ${X}/f",
            'echo x > "$F"',
            "echo x > $(echo f)",
            "echo x > `echo f`",
            "touch .{claude,x}/s",
            "mkdir -p a/{b,c}",
            "touch .c[l]aude/s",
            "touch ~+/.claude/s",
            "touch ~-/x",
            "dd if=/dev/zero of=$F",
            "cp x --target-directory=.claude",
            "cp x -t.claude",
            "cp x -t .claude",
            "install -t.claude x",
        ):
            self.blocked(command)

    def test_relative_writes_after_an_unknown_cd_are_blocked(self):
        for command in (
            'cd "$X" && touch f',
            "cd - && echo x > f",
            "popd && touch f",
            "pushd src && popd && touch .claude/x",
            "cd ~- && touch f",
        ):
            self.blocked(command)
        for command in ('cd "$X" && touch /tmp/f', "popd && echo x > /tmp/f", "cd src && touch f"):
            self.allowed(command)

    def test_sed_scripts_with_dollar_signs_are_not_targets(self):
        for command in (
            "sed -i 's/$/x/' notes.txt",
            "sed -i '$d' notes.txt",
            "sed -i -e 's/a$/b/' notes.txt",
            "sed -i.bak -e '$d' notes.txt",
            'sed -i "s/$X/y/" notes.txt',
            "sed -i --expression='s/$/x/' notes.txt",
        ):
            self.quiet(command)
        self.blocked("sed -i '$d' .claude/settings.json")
        self.blocked("sed -i -e 's/a/b/' $F")

    def test_harmless_literal_paths_still_pass(self):
        for command in (
            "cp a.txt b.txt",
            "mkdir -p out/x",
            "touch notes.txt",
            "cp src/a.cs /tmp/x",
            "chmod +x scripts/run.sh",
            "tee out.log",
            "install -m 755 a /tmp/b",
            "find . -name x -exec cp {} out/ \\;",  # find -exec with a path outside the repo is already blocked.
            "dd if=/dev/zero of=/tmp/blob bs=1 count=1",
        ):
            self.quiet(command)

    def test_tilde_forms_the_guard_cannot_resolve_are_not_deletable(self):
        # The repo is the process's own directory: a path left as "~-" would wrongly resolve inside it.
        ctx = g.Context(repo=os.getcwd(), cwd=os.getcwd(), agent_type=None, scratchpad=SCRATCH, publish_dir=PUBLISH)
        for command in ("rm -r -f ~-/x", "rm -rf ~+", "rm -rf ~+/x", "rm ~-", "rm -rf ~nosuchuser_zz/x"):
            self.assertIsNotNone(g.evaluate(command, ctx), command)

    def test_a_symlink_into_the_team_rules_is_followed_for_writes_not_for_rm(self):
        repo = Path(os.path.realpath(tempfile.mkdtemp(prefix="guard-link-")))
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text("{}")
        (repo / "link").symlink_to(".claude/settings.json")
        (repo / "dirlink").symlink_to(".claude")
        ctx = g.Context(repo=str(repo), cwd=str(repo), agent_type=None, scratchpad=SCRATCH, publish_dir=PUBLISH, protected=PROTECTED)

        for command in ("echo x > link", "echo x >> link", "cp /tmp/x link", "touch link", "tee link", "sed -i s/a/b/ link",
                        "chmod 600 link", "truncate -s 0 link", "echo x > dirlink/new"):
            self.assertIsNotNone(g.evaluate(command, ctx), command)
        for command in ("rm link", "rm dirlink", "mv link /tmp/moved", "ln -s other link2"):
            self.assertIsNone(g.evaluate(command, ctx), command)

    def test_writes_into_git_hooks_and_config_are_blocked(self):
        for command in (
            "echo x > .git/hooks/post-commit",
            "cp /tmp/x .git/hooks/pre-push",
            "chmod +x .git/hooks/post-commit",
            "touch .git/hooks/x",
            "tee .git/config",
            "echo x >> .git/config",
            "sed -i s/a/b/ .git/config",
            "cd .git && echo x > config",
            "cd .git/hooks && touch x",
        ):
            self.blocked(command)
        for command in ("rm .git/index.lock", "cat .git/config", "echo x > .git/info/exclude", "git config user.name x"):
            self.allowed(command)

    def test_a_quoted_or_escaped_redirection_symbol_is_a_word_and_hides_nothing(self):
        for command in (
            "rm -rf '<' /home/ubuntu/somedir",
            'rm -rf "<" /home/ubuntu/somedir',
            "rm -rf \\< /home/ubuntu/somedir",
            "rm -rf '>' /home/ubuntu/somedir",
            "rm -rf '<<<' /home/ubuntu/somedir",
            "rm -rf '>>' /home/ubuntu/somedir",
            "cp x '<<<' .claude/",
            "cp x '>' .claude/settings.json",
            "cp x \\> .claude/settings.json",
            "cp x '<' .claude/settings.json",
            "cp x < in.txt .claude/settings.json",
            "rm -rf < in.txt /home/ubuntu/somedir",
        ):
            self.blocked(command)
        for command in ("rm -rf '<' somedir", "echo '>' notes.txt", "echo \\> notes.txt", "echo '<<<' > out.txt"):
            self.blocked(command)  # Round 2: a quoted or escaped < or > is refused, these were allowed.
        self.allowed("cat < in.txt")

    def test_a_number_is_a_file_descriptor_only_when_glued_to_the_operator(self):
        for command in (
            "timeout 5 >/dev/null cp x .claude/settings.json",
            "timeout 5 > /dev/null cp x .claude/settings.json",
            "nice -n 5 >/dev/null sudo true",
            "flock 9 >/dev/null rm -rf /home/ubuntu/x",
            "timeout 5 2>/dev/null rm -rf /home/ubuntu/x",
            "echo x 5>.claude/x",
            "echo x 12> .claude/x",
        ):
            self.blocked(command)
        for command in (
            "cmd 2>&1",
            "dotnet test 2> err.txt",
            "exec 5> out.txt",
            "echo hi 2>&1 >/dev/null",
            "(echo hi 2>/dev/null)",
            "echo a; 2>err.txt echo b",
            "timeout 5 >/dev/null cp x out.txt",
            "cat 0< in.txt",
        ):
            self.quiet(command)
        self.blocked("echo a;2>err.txt echo b")  # Round 2: a number glued to a separator is refused, was allowed.

    def test_pushd_without_a_plain_directory_leaves_the_directory_unknown(self):
        for command in (
            "pushd -n /tmp && echo x > .claude/settings.json",
            "pushd /tmp && pushd && echo x > .claude/settings.json",
            "pushd && echo x > f",
            "pushd +1 && echo x > f",
            "pushd -1 && echo x > f",
            "pushd -- /tmp && echo x > f",
        ):
            self.blocked(command)
        for command in ("pushd /tmp && echo x > /tmp/y", "pushd /tmp && echo x > y"):
            self.allowed(command)

    def test_a_directory_destination_is_combined_with_each_source_name(self):
        for command in (
            "cp -r /somewhere/hooks .git/",
            "cp -r /somewhere/.claude ./",
            "cp -r /somewhere/.claude .",
            "cp -r /somewhere/.claude/ ./",
            "mv /somewhere/hooks .git/",
            "ln -s /somewhere/hooks .git/",
            "install /somewhere/hooks .git/",
            "rsync -a /somewhere/hooks .git/",
            "cp -t .git/ /somewhere/hooks",
            "cp --target-directory=.git /somewhere/hooks",
            "cp -r /somewhere/.claude src/..",
        ):
            self.blocked(command)
        for command in ("cp a.txt out/", "cp -r src out/", "cp a.txt b.txt c.txt out/", "cp -t out/ a.txt", "mv a.txt out/"):
            self.quiet(command)


class WriteTargetsOnDisk(unittest.TestCase):
    """Glob and ask-formatting tests that need a real directory."""

    def setUp(self):
        self.repo = Path(os.path.realpath(tempfile.mkdtemp(prefix="guard-glob-")))
        self.addCleanup(shutil.rmtree, self.repo, True)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
        for name in (".claude/settings.json", ".claude/agents/a.md", "CLAUDE.md", "deploy/run.sh", "src/a.cs", "src/b.cs", "notes.txt", "a1.txt"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")

    def verdict(self, command):
        ctx = g.Context(repo=str(self.repo), cwd=str(self.repo), agent_type=None, scratchpad=SCRATCH, publish_dir=PUBLISH, protected=PROTECTED)
        return g.evaluate_full(command, ctx)

    def assertDenied(self, command):
        self.assertIsNotNone(self.verdict(command).deny, f"should be blocked: {command!r}")

    def assertPasses(self, command):
        verdict = self.verdict(command)
        self.assertEqual((verdict.deny, verdict.ask), (None, None), command)

    def test_a_glob_that_matches_the_team_rules_is_blocked(self):
        for command in (
            "touch .cl*/settings.json",
            "cp /tmp/x .c?aude/settings.json",
            "touch .claude/*",
            "echo x > .cl*/settings.json",
            "chmod 600 .cl*/agents/*.md",
            "tee .*/settings.json",
        ):
            self.assertDenied(command)

    def test_a_glob_that_matches_protected_code_asks(self):
        for command in ("touch C*.md", "chmod 600 d?ploy/run.sh", "echo x > CLAU*", "touch dep*/*"):
            verdict = self.verdict(command)
            self.assertIsNone(verdict.deny, command)
            self.assertIsNotNone(verdict.ask, command)

    def test_ordinary_globs_pass(self):
        for command in ("touch src/*.cs", "cp *.txt /tmp/x", "chmod +x src/?.cs", "touch s*/x.cs", "cp a?.txt /tmp/x", "touch nomatch*/x"):
            self.assertPasses(command)

    def test_a_glob_with_too_many_matches_is_blocked(self):
        big = self.repo / "big"
        big.mkdir()
        for number in range(500):
            (big / f"f{number:03d}").write_text("")
        self.assertPasses("touch big/f*")

        (big / "f500").write_text("")

        self.assertDenied("touch big/f*")

    def test_every_ask_reason_is_shown_once_in_order(self):
        ask = self.verdict("touch CLAUDE.md && touch deploy/x && touch CLAUDE.md && git push").ask

        self.assertIsNotNone(ask)
        self.assertEqual(ask.count("'CLAUDE.md'"), 1)
        self.assertLess(ask.index("'CLAUDE.md'"), ask.index("'deploy/x'"))
        self.assertLess(ask.index("push"), ask.index("'CLAUDE.md'"))  # Gate reasons (push, share) come first.

        many = self.verdict("touch deploy/a deploy/b deploy/c deploy/d deploy/e deploy/f deploy/g").ask
        self.assertIn("'deploy/e'", many)
        self.assertNotIn("'deploy/f'", many)
        self.assertIn("and 2 more", many)

    def test_the_ask_reason_is_capped_and_sanitised(self):
        ask = self.verdict('touch "deploy/' + "x" * 300 + '"').ask
        self.assertIsNotNone(ask)
        self.assertNotIn("x" * 130, ask)

        crafted = self.verdict('touch "deploy/a\nSECOND LINE\x1b31m b"').ask
        self.assertIsNotNone(crafted)
        for bad in ("\n", "\x1b", " ", "\r"):
            self.assertNotIn(bad, crafted)

    def test_the_push_is_always_named_even_after_many_other_reasons(self):
        ask = self.verdict("touch deploy/a deploy/b deploy/c deploy/d deploy/e deploy/f && git push").ask

        self.assertIsNotNone(ask)
        self.assertIn("git push", ask)
        self.assertLess(ask.index("git push"), ask.index("'deploy/a'"))
        self.assertIn("and 2 more", ask)

    def test_a_quote_in_a_shown_name_cannot_end_the_quoting(self):
        ask = self.verdict("touch \"deploy/a'b\" 'deploy/c\\d'").ask

        self.assertIsNotNone(ask)
        self.assertIn("'deploy/a\\'b'", ask)
        self.assertIn("'deploy/c\\\\d'", ask)

    def test_a_directory_destination_that_exists_is_combined_with_the_source_name(self):
        for command in ("cp -r /somewhere/hooks .git", "mv /somewhere/hooks .git", "cp -r /somewhere/.claude .", "cp /somewhere/hooks ./.git"):
            self.assertDenied(command)
        self.assertPasses("cp /somewhere/a.txt src")
        self.assertPasses("cp a.txt src/")
        self.assertPasses("cp -r src /tmp")

    def test_a_directory_symlink_destination_is_followed_by_mv_and_ln_too(self):
        (self.repo / "dirlink").symlink_to(".claude")
        for command in ("mv /somewhere/x dirlink", "ln -s /somewhere/x dirlink/", "cp /somewhere/x dirlink"):
            self.assertDenied(command)

    def test_a_wildcard_outside_the_repo_is_expanded_and_links_into_the_rules_are_found(self):
        outside = Path(os.path.realpath(tempfile.mkdtemp(prefix="guard-outside-")))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "link").symlink_to(self.repo / ".claude" / "settings.json")
        (outside / "d").symlink_to(self.repo / ".claude")
        (outside / "a.log").write_text("x")
        (outside / "b.log").write_text("x")

        for command in (
            f"echo x > {outside}/l*",
            f"echo x > {outside}/?/settings.json",
            f"echo x > {outside}/?/brand-new",
            f"touch {outside}/d*/brand-new",
            f"cp x {outside}/li?k",
            f"tee {outside}/*",
        ):
            self.assertDenied(command)
        for command in (f"touch {outside}/*.log", f"echo x > {outside}/a.l*", f"cp x {outside}/?.log"):
            self.assertPasses(command)

    def test_a_wildcard_with_more_than_500_matches_outside_the_repo_is_blocked_too(self):
        outside = Path(os.path.realpath(tempfile.mkdtemp(prefix="guard-outside-")))
        self.addCleanup(shutil.rmtree, outside, True)
        for number in range(501):
            (outside / f"f{number:03d}").write_text("")

        self.assertDenied(f"touch {outside}/*")
        self.assertPasses(f"touch {outside}/f00*")


class RedirectHookProcess(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="guard-redirect-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "protected-paths.txt").write_text("deploy/**\nCLAUDE.md\n")

    def run_hook(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.repo)}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env={"CLAUDE_PROJECT_DIR": str(self.repo), "PATH": "/usr/bin"})

    def test_a_redirect_into_the_rules_exits_2_and_into_protected_code_asks(self):
        blocked = self.run_hook("echo x > .claude/settings.json")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn(".claude", blocked.stderr)

        asked = self.run_hook("echo x > CLAUDE.md")
        self.assertEqual((asked.returncode, asked.stderr), (0, ""))
        decision = json.loads(asked.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("CLAUDE.md", decision["permissionDecisionReason"])

        quiet = self.run_hook("echo x > notes.txt 2>&1")
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""))


class FailClosed(Cases):
    """T-013a round 2: a raw-text check blocks what the guard cannot classify, before any parsing."""

    MESSAGE = "the guard cannot classify this command; write it plainly"

    def check(self, command):
        return g.evaluate(command, context())

    def test_a_quoted_or_escaped_operator_blocks_the_whole_command(self):
        for command in (
            "rm -rf '<' /home/ubuntu/somedir",
            "cp x '<<<' .claude/",
            "echo '>'",
            "tee '|' .claude/settings.json",
            "rm -rf ';' /home/ubuntu/projects/other",
            "echo a\\;b",
            'echo "a|b"',
            "echo '<<EOF'\ncp x .claude/settings.json\nEOF",
            "grep 'x|y' f",
            "sed 's/a;b/c/' f",
            "awk '$1>2' f",
            'git commit -m "fix (x)"',
            "bash -c 'a && b'",
            'echo "a & b"',
            "echo \\< x",
            "echo \\( x",
        ):
            self.assertEqual(self.check(command), self.MESSAGE, command)

    def test_a_number_or_ampersand_glued_before_a_redirect_blocks(self):
        for command in (
            "true&&2>/dev/null sudo -n true",
            "true&&2>/dev/null rm -rf /home/ubuntu/projects/other",
            "true&2>/dev/null cp x .claude/settings.json",
            "true;2>/dev/null sudo -n true",
            "true|2>/dev/null sudo -n true",
            "echo a;2>err.txt echo b",
            "(2>f echo b)",
            "true&&>f echo b",
        ):
            self.assertEqual(self.check(command), self.MESSAGE, command)

    def test_ansi_c_and_locale_quoting_blocks(self):
        for command in ("echo $'\\'' > .claude/settings.json #'", 'echo $"x"', "echo $'a'", "echo \"$'\""):
            self.assertEqual(self.check(command), self.MESSAGE, command)

    def test_a_backslash_newline_blocks(self):
        for command in ("echo x > .cl\\\naude/settings.json", "su\\\ndo true", 'echo "a\\\nb"'):
            self.assertEqual(self.check(command), self.MESSAGE, command)

    def test_a_comment_blocks(self):
        for command in (
            "echo hi #'\necho x > .claude/settings.json\n#'",
            "echo hi # note",
            "# note\necho hi",
            "echo hi;# note",
            "echo hi&&#x",
            "echo hi >#x",
            "bash -c 'echo hi # x'",
        ):
            self.assertEqual(self.check(command), self.MESSAGE, command)

    def test_find_exec_may_end_with_an_escaped_semicolon_only_as_its_last_word(self):
        self.assertEqual(self.check("find . -exec rm {} \\; -print"), self.MESSAGE)
        self.assertEqual(self.check("find . -exec rm {} \\; -exec rm {} \\;"), self.MESSAGE)
        self.assertEqual(self.check("echo x \\;"), self.MESSAGE)
        self.assertEqual(self.check("find . -name x \\;"), self.MESSAGE)
        self.assertEqual(self.check("find . -exec a\\;b {}"), self.MESSAGE)

    def test_plain_commands_still_pass_without_asking(self):
        for command in (
            "find . -name x -exec cp {} out/ \\;",
            "find . -name '*.py' -exec grep -l x {} +",
            "find . -name x -exec cp {} out/ \\; && echo done",
            "cmd 2>&1 | tail",
            "dotnet test 2> err.txt",
            "dotnet test > out.txt",
            "ls > /dev/null 2>&1",
            'git commit -m "fix #12"',
            "echo a#b",
            "git log --format='%h %s'",
            "cat <<EOF > /tmp/x\nit's \"quoted\"; a | b > c # d\nEOF",
            "cat <<'EOF' > /tmp/x\nit's \"quoted\"; a | b > c # d\nEOF",
            "echo hi &> /dev/null",
            "echo hi >&2",
            "echo ${#PATH} $#",
        ):
            self.allowed(command)
            self.assertIsNone(g.evaluate_ask(command, context()), f"should not ask: {command!r}")

    def test_a_heredoc_body_is_text_but_the_lines_after_it_are_commands(self):
        self.allowed("cat <<EOF > /tmp/x\nhi; there 'x\nEOF\necho done")
        self.assertEqual(self.check("cat <<EOF > /tmp/x\nbody\nEOF\necho hi # x"), self.MESSAGE)
        self.assertEqual(self.check("cat <<EOF > /tmp/x\nbody\nEOF\necho 'a|b'"), self.MESSAGE)
        self.blocked("cat <<EOF > /tmp/x\nbody\nEOF\ncp x .claude/settings.json")

    def test_a_heredoc_that_feeds_a_shell_is_checked_as_a_script(self):
        self.blocked("bash <<EOF\ncp x .claude/settings.json\nEOF")
        self.blocked("sh <<'EOF'\nsudo id\nEOF")
        self.assertEqual(self.check("bash <<EOF\necho 'a|b'\nEOF"), self.MESSAGE)

    def test_the_commit_message_heredoc_form_is_blocked_use_a_file_instead(self):
        self.assertEqual(self.check('git commit -m "$(cat <<\'EOF\'\nAdd the thing\nEOF\n)"'), self.MESSAGE)
        self.allowed("git commit -F msg.txt")


class GateCodeScriptTests(Cases):
    """T-011b: the fixed gate-code script runs without a block or a question; the old inline command stays blocked."""

    MESSAGE = "the guard cannot classify this command; write it plainly"
    OLD_INLINE = "python3 -I -c \"import secrets; print(''.join(secrets.choice('23456789ABCDEFGHJKMNPQRSTUVWXYZ') for _ in range(6)))\""

    def test_the_script_by_relative_or_absolute_path_is_neither_blocked_nor_asked(self):
        for command in (".claude/scripts/gate-code", f"{REPO}/.claude/scripts/gate-code"):
            for agent in (None, "manager", "builder"):
                self.assertIsNone(g.evaluate(command, context(agent_type=agent)), command)
                self.assertIsNone(g.evaluate_ask(command, context(agent_type=agent)), command)

    def test_chained_after_the_script_the_second_command_is_still_checked(self):
        self.blocked(".claude/scripts/gate-code; rm -rf /")
        self.blocked(".claude/scripts/gate-code && rm -rf /tmp/x")
        self.blocked(".claude/scripts/gate-code && sudo id")
        self.blocked(".claude/scripts/gate-code && cp x .claude/settings.json")

    def test_the_old_inline_command_is_still_refused_as_unclassifiable(self):
        self.assertEqual(g.evaluate(self.OLD_INLINE, context()), self.MESSAGE)
        self.assertEqual(g.evaluate(self.OLD_INLINE, context(agent_type="manager")), self.MESSAGE)


if __name__ == "__main__":
    unittest.main()
