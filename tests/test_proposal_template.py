"""Runs the proposal template's own bash blocks in a throwaway git repository. Run: python3 -m unittest discover -s tests"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "proposal.md").read_text()
BRANCH = "task/T-9-demo"
P = "docs/proposed/demo"


def block(n):
    """The bash block under '## Step n:'."""
    section = re.search(rf"^## Step {n}:.*?^```bash\n(.*?)^```", TEMPLATE, re.S | re.M)
    assert section, f"no bash block in step {n}"
    return section.group(1)


class Repo:
    """A throwaway repo: main has .claude/agents/old.md; the task branch adds a proposal folder at commit C."""

    def __init__(self, base_mode=0o644):
        self.tmp = Path(tempfile.mkdtemp(prefix="proposal-test-"))
        self.dir = self.tmp / "repo"
        self.dir.mkdir()
        (self.tmp / "home").mkdir()
        (self.tmp / "tmpdir").mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "PAGER": "cat", "LC_ALL": "C", "TMPDIR": str(self.tmp / "tmpdir"),
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.git("init", "-q", "-b", "main")
        self.write(".gitignore", "ignored.txt\n")
        self.write(".claude/agents/old.md", "old\n", base_mode)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "-b", BRANCH)
        self.write(f"{P}/README.md", "readme\n")
        self.write(f"{P}/old.md", "new content\n")
        self.write(f"{P}/tool.sh", "#!/bin/sh\necho hi\n", 0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "proposal")
        self.c = self.git("rev-parse", "HEAD").strip()
        self.map = "0644 old.md .claude/agents/old.md\n0755 tool.sh .claude/scripts/new/tool.sh"

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, env=self.env, check=True, capture_output=True, text=True).stdout

    def write(self, rel, text, mode=0o644):
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def path(self, rel):
        return self.dir / rel

    def run(self, steps, c=None, base=None, branch=BRANCH, map_=None, between=None, test_cmd="true", interrupt_step_2=False,
            cwd=None, prefix=""):
        """Run the filled-in blocks of the given steps in one bash process, like the boss's terminal."""
        step0 = block(0)
        values = {"C": c or self.c, "BASE": base or self.base, "B": branch, "P": P}
        for name, value in values.items():
            step0 = re.sub(rf"(?m)^{name}=.*$", lambda _m, n=name, v=value: f"{n}={v}", step0)
        step0 = re.sub(r"(?m)^MAP='.*'$", lambda _m: "MAP='" + (map_ or self.map) + "'", step0)
        pieces = [prefix] if prefix else []
        for n in steps:
            if n == 0:
                pieces.append(step0)
            elif n == 2:
                viewer = "| sh -c 'kill -TERM $PPID'" if interrupt_step_2 else "| cat"
                pieces.append(block(2).replace("| less", viewer))
            elif n == 4:
                pieces.append(re.sub(r"(?m)^env -u .*$", lambda _m: test_cmd, block(4)))
            else:
                pieces.append(block(n))
            if between and n == between[0]:
                pieces.append(between[1])
        done = subprocess.run(["bash", "-c", "\n".join(pieces)], cwd=cwd or self.dir, env=self.env, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

    def leftovers(self):
        """Files left in the TMPDIR the steps used."""
        return sorted(p.name for p in (self.tmp / "tmpdir").iterdir())

    def mode(self, rel):
        return stat.S_IMODE(self.path(rel).stat().st_mode)


class ProposalBehaviour(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def assertStopsWithNothingInstalled(self, out, *needles):
        self.assertIn("STOP:", out)
        self.assertNotIn("installed ", out)
        self.assertNotIn("OK: step 3", out)
        for needle in needles:
            self.assertIn(needle.lower(), out.lower())
        self.assertEqual((self.repo.path(".claude/agents/old.md")).read_text(), "old\n")
        self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists())

    def test_b1_happy_path_installs_exact_bytes_and_modes(self):
        out = self.repo.run([0, 1, 3])
        self.assertNotIn("STOP:", out)
        self.assertIn("OK: step 0", out)
        self.assertIn("OK: step 1", out)
        self.assertIn("OK: step 3", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "new content\n")
        self.assertEqual(self.repo.path(".claude/scripts/new/tool.sh").read_text(), "#!/bin/sh\necho hi\n")
        self.assertEqual(self.repo.mode(".claude/agents/old.md"), 0o644)
        self.assertEqual(self.repo.mode(".claude/scripts/new/tool.sh"), 0o755)

    def test_b1b_step_2_shows_the_diff_and_changes_nothing(self):
        out = self.repo.run([0, 1, 2])
        self.assertIn("+new content", out)
        self.assertIn("== .claude/scripts/new/tool.sh (0755)", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")

    def test_b2_a_git_ignored_file_in_the_proposal_folder_stops_it(self):
        self.repo.write(f"{P}/ignored.txt", "x\n")
        self.assertStopsWithNothingInstalled(self.repo.run([0, 1, 3]), "git-ignored")

    def test_b3_an_untracked_file_in_the_proposal_folder_stops_it(self):
        self.repo.write(f"{P}/extra.md", "x\n")
        self.assertStopsWithNothingInstalled(self.repo.run([0, 1, 3]), "untracked")

    def test_b4_a_working_tree_edit_after_the_reviewed_commit_stops_it(self):
        self.repo.write(f"{P}/old.md", "sneaky\n")
        self.assertStopsWithNothingInstalled(self.repo.run([0, 1, 3]), "has changed")

    def test_b4b_a_later_commit_that_edits_the_proposal_stops_it(self):
        self.repo.write(f"{P}/old.md", "sneaky\n")
        self.repo.git("commit", "-q", "-am", "late edit")
        self.assertStopsWithNothingInstalled(self.repo.run([0, 1, 3]), "differs from the reviewed commit")

    def test_b5_a_map_entry_that_is_not_a_tracked_file_stops_it(self):
        for src in ("ignored.txt", "missing.md", "../../../.gitignore", "."):
            with self.subTest(src=src):
                out = self.repo.run([0, 1, 3], map_=f"0644 {src} .claude/agents/x.md")
                self.assertStopsWithNothingInstalled(out)
                self.assertTrue("not a file tracked" in out or "unsafe source path" in out, out)
                self.assertFalse(self.repo.path(".claude/agents/x.md").exists())

    def test_b5b_the_message_names_the_untracked_source(self):
        out = self.repo.run([0], map_="0644 missing.md .claude/agents/x.md")
        self.assertIn("is not a file tracked at the reviewed commit", out)

    def test_b6_a_wrong_branch_or_bad_hash_stops_it(self):
        cases = {"wrong branch": dict(branch="task/other"), "short hash": dict(c=self.repo.c[:12]),
                 "unknown hash": dict(c="0" * 40)}
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.assertStopsWithNothingInstalled(self.repo.run([0, 1, 3], **kwargs))

    def test_b6b_a_commit_that_is_not_in_this_branch_stops_it(self):
        self.repo.git("checkout", "-q", "main")
        self.repo.git("checkout", "-q", "-b", "elsewhere")
        self.repo.write("other.txt", "x\n")
        self.repo.git("add", "other.txt")
        self.repo.git("commit", "-q", "-m", "elsewhere")
        other = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("checkout", "-q", BRANCH)
        out = self.repo.run([0, 1, 3], c=other)
        self.assertStopsWithNothingInstalled(out, "does not contain")

    def test_b7_dangerous_targets_and_modes_stop_it_with_the_right_reason(self):
        outside = self.repo.tmp / "outside.md"
        cases = {"0644 old.md ../escape.md": "unsafe target", f"0644 old.md {outside}": "unsafe target",
                 "0644 old.md .git/hooks/pre-commit": "unsafe target", "0644 old.md ./.git/hooks/pre-commit": "unsafe target",
                 "0644 old.md a/./b": "unsafe target", "0644 old.md a/.": "unsafe target", "0644 old.md -rf": "unsafe target",
                 "0644 old.md .claude/a$b": "unsafe target", "0644 old.md .claude/a;b": "unsafe target",
                 "0644 old.md .claude/*.md": "unsafe target", "0644 old.md .claude//x": "unsafe target",
                 "0644 old.md .claude/x/": "unsafe target", "0644 old.md sub/.git/x": "unsafe target",
                 "0644 -x .claude/agents/x.md": "unsafe source path", "0644 ../old.md .claude/agents/x.md": "unsafe source path",
                 "0777 old.md .claude/agents/x.md": "mode 0777", "4755 tool.sh .claude/agents/x.md": "mode 4755",
                 "0644 old.md a b c": "bad MAP line"}
        for line, reason in cases.items():
            with self.subTest(line=line):
                out = self.repo.run([0, 1, 3], map_=line)
                self.assertStopsWithNothingInstalled(out, reason)
        self.assertFalse(outside.exists())
        self.assertFalse((self.repo.tmp / "escape.md").exists())
        self.assertFalse(self.repo.path(".git/hooks/pre-commit").exists())
        self.assertFalse(self.repo.path("-rf").exists())

    def test_b8_targets_that_changed_since_base_stop_it(self):
        with self.subTest("edited in the working tree"):
            self.repo.write(".claude/agents/old.md", "local edit\n")
            out = self.repo.run([0, 1, 3])
            self.assertIn("changed since BASE", out)
            self.assertNotIn("installed ", out)
            self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "local edit\n")
            self.repo.git("checkout", "-q", "--", ".claude/agents/old.md")
        with self.subTest("changed by a later commit"):
            self.repo.write(".claude/agents/old.md", "committed edit\n")
            self.repo.git("commit", "-q", "-am", "later")
            out = self.repo.run([0, 1, 3])
            self.assertIn("changed since BASE", out)
            self.assertNotIn("installed ", out)
            self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists())

    def test_b8b_a_new_target_that_already_exists_stops_it(self):
        self.repo.write(".claude/scripts/new/tool.sh", "squatter\n")
        out = self.repo.run([0, 1, 3])
        self.assertIn("should be new but already exists", out)
        self.assertNotIn("installed ", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")

    def test_b8c_a_symlink_target_stops_it(self):
        elsewhere = self.repo.tmp / "elsewhere.md"
        elsewhere.write_text("keep\n")
        self.repo.path(".claude/scripts").mkdir(parents=True)
        self.repo.path(".claude/scripts/new").mkdir()
        self.repo.path(".claude/scripts/new/tool.sh").symlink_to(elsewhere)
        out = self.repo.run([0, 1, 3])
        self.assertIn("is a symlink", out)
        self.assertNotIn("installed ", out)
        self.assertEqual(elsewhere.read_text(), "keep\n")

    def test_b9_an_edit_between_step_1_and_step_3_stops_the_install(self):
        out = self.repo.run([0, 1, 3], between=(1, f"echo sneaky > {P}/old.md"))
        self.assertStopsWithNothingInstalled(out, "has changed")

    def test_b9b_a_new_ignored_file_between_step_1_and_step_3_stops_the_install(self):
        out = self.repo.run([0, 1, 3], between=(1, f"echo x > {P}/ignored.txt"))
        self.assertStopsWithNothingInstalled(out, "git-ignored")

    def test_b10_rollback_restores_content_and_mode_removes_new_files_and_leaves_a_clean_status(self):
        repo = Repo(base_mode=0o755)
        self.addCleanup(repo.close)
        out = repo.run([0, 1, 3, 5], between=(3, "test $(stat -c %a .claude/agents/old.md) = 644 || echo MODE-NOT-INSTALLED"))
        self.assertIn("OK: step 3", out)
        self.assertNotIn("MODE-NOT-INSTALLED", out)
        self.assertEqual(repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertTrue(repo.mode(".claude/agents/old.md") & 0o100, "the executable bit comes back")
        self.assertFalse(repo.path(".claude/scripts/new/tool.sh").exists())
        self.assertEqual(repo.git("status", "--short", "--ignored"), "")
        self.assertNotIn("STOP:", out)

    def test_b10b_rollback_after_an_install_that_stopped_part_way(self):
        # the second file cannot be installed because its parent directory is read-only: step 3 stops after the first
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.repo.path(".claude/scripts").mkdir(parents=True)
        self.repo.path(".claude/scripts").chmod(0o555)
        self.addCleanup(self.repo.path(".claude/scripts").chmod, 0o755)
        out = self.repo.run([0, 1, 3])
        self.assertIn("installed .claude/agents/old.md", out)
        self.assertIn("STOP: install stopped part-way; run step 5", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "new content\n")
        self.repo.run([0, 5])
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertEqual(self.repo.git("status", "--short"), "")
        self.assertEqual(self.repo.leftovers(), [], "step 3 left its temp file behind")

    def test_b11_local_git_config_that_can_run_programs_stops_it(self):
        keys = {"alias.x": "status", "core.fsmonitor": "false", "core.hooksPath": "/nonexistent", "core.sshCommand": "ssh",
                "core.pager": "cat", "core.editor": "true", "filter.lfs.clean": "cat", "diff.foo.textconv": "cat",
                "include.path": "/nonexistent.cfg", "includeIf.gitdir:/x/.path": "/nonexistent.cfg"}
        for key, value in keys.items():
            with self.subTest(key):
                self.repo.git("config", "--local", key, value)
                out = self.repo.run([0, 1, 3])
                self.assertStopsWithNothingInstalled(out, key.split(":")[0].split("/")[0])
                self.repo.git("config", "--local", "--unset-all", key)
        # the same repository passes once the settings are gone
        self.assertIn("OK: step 3", self.repo.run([0, 1, 3]))

    def test_b11b_the_test_repo_reads_no_git_config_outside_its_own(self):
        listing = self.repo.git("config", "--list", "--show-origin")
        origins = {line.split("\t")[0] for line in listing.splitlines()}
        self.assertEqual(origins, {"file:.git/config"})


class GitProgramsStayOff(unittest.TestCase):
    """Hooks, and a core.fsmonitor program from the global config, must never run during the steps."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)
        self.marker = self.repo.tmp / "marker"
        script = self.repo.tmp / "evil.sh"
        script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {self.marker}\nexit 0\n")
        script.chmod(0o755)
        for hook in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-merge", "post-checkout"):
            self.repo.write(f".git/hooks/{hook}", f"#!/bin/sh\necho {hook} >> {self.marker}\n", 0o755)
        config = self.repo.tmp / "global.cfg"
        config.write_text(f"[core]\n\tfsmonitor = {script}\n")
        self.repo.env["GIT_CONFIG_GLOBAL"] = str(config)

    def test_the_planted_programs_do_run_when_git_is_called_bare(self):
        # guards the test itself: with no protection the fsmonitor program and a hook both fire
        subprocess.run(["git", "status"], cwd=self.repo.dir, env=self.repo.env, check=True, capture_output=True)
        self.assertTrue(self.marker.exists(), "fsmonitor program did not run: the test proves nothing")
        self.marker.unlink()
        self.repo.write("x.txt", "x\n")
        self.repo.git("add", "x.txt")
        self.repo.git("commit", "-q", "-m", "x")
        self.assertIn("pre-commit", self.marker.read_text())

    def test_steps_0_to_4_run_no_hook_and_no_fsmonitor(self):
        out = self.repo.run([0, 1, 2, 3, 4])
        self.assertIn("OK: step 3", out)
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

    def test_steps_6_and_7_run_no_hook_and_no_fsmonitor(self):
        out = self.repo.run([0, 1, 3, 4, 6, 7])
        self.assertIn("OK: step 7", out)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "main").split()[0], "Merge")
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

    def test_the_hooks_do_fire_on_a_bare_commit_merge_and_checkout(self):
        # guards the test: the post-commit, post-merge and post-checkout hooks are live in this repository
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "--no-edit", BRANCH)
        self.repo.write("y.txt", "y\n")
        self.repo.git("add", "y.txt")
        self.repo.git("commit", "-q", "-m", "y")
        text = self.marker.read_text()
        for hook in ("post-checkout", "post-merge", "pre-commit", "commit-msg", "post-commit"):
            self.assertIn(hook, text, hook)

    def test_the_rollback_runs_no_hook_and_no_fsmonitor(self):
        out = self.repo.run([0, 1, 3, 5])
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertFalse(self.marker.exists(), out)


class MainMoved(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def on_main(self, rel, text):
        self.repo.git("checkout", "-q", "main")
        self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "main moves")
        self.repo.git("checkout", "-q", BRANCH)

    def test_c1_a_replaced_target_edited_on_main_after_base_stops_it(self):
        self.on_main(".claude/agents/old.md", "edited on main\n")
        out = self.repo.run([0, 1, 3])
        self.assertIn("changed on main since BASE", out)
        self.assertNotIn("installed ", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists())

    def test_c2_a_new_target_created_on_main_after_base_stops_it(self):
        self.on_main(".claude/scripts/new/tool.sh", "created on main\n")
        self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists(), "not on this branch")
        out = self.repo.run([0, 1, 3])
        self.assertIn("exists on main", out)
        self.assertNotIn("installed ", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")

    def test_c3_a_replaced_target_deleted_on_main_stops_it(self):
        self.repo.git("checkout", "-q", "main")
        self.repo.git("rm", "-q", ".claude/agents/old.md")
        self.repo.git("commit", "-q", "-m", "gone")
        self.repo.git("checkout", "-q", BRANCH)
        self.assertIn("changed on main since BASE", self.repo.run([0, 1, 3]))

    def test_c4_main_moving_elsewhere_does_not_stop_it(self):
        self.on_main("unrelated.txt", "x\n")
        self.assertIn("OK: step 3", self.repo.run([0, 1, 3]))


class VerifyAndFinish(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def test_b1_verify_passes_after_a_clean_install(self):
        out = self.repo.run([0, 1, 3, 4])
        self.assertIn("OK: installed files match the reviewed commit", out)
        self.assertNotIn("STOP:", out)

    def test_b2_an_edit_to_an_installed_file_after_step_3_stops_step_4(self):
        out = self.repo.run([0, 1, 3, 4], between=(3, "echo tampered >> .claude/agents/old.md"))
        self.assertIn("STOP: .claude/agents/old.md is not the reviewed content", out)
        self.assertNotIn("OK: installed files", out)

    def test_b3_a_mode_change_after_step_3_stops_step_4(self):
        out = self.repo.run([0, 1, 3, 4], between=(3, "chmod 0755 .claude/agents/old.md"))
        self.assertIn("STOP: .claude/agents/old.md has mode 755, expected 644", out)

    def test_b4_a_symlink_swapped_in_after_step_3_stops_step_4(self):
        out = self.repo.run([0, 1, 3, 4], between=(3, "cp .claude/agents/old.md ../same.md; rm .claude/agents/old.md; ln -s ../../../same.md .claude/agents/old.md"))
        self.assertIn("STOP: .claude/agents/old.md is a symlink", out)

    def test_b5_an_empty_file_where_a_target_should_be_stops_verify(self):
        out = self.repo.run([0, 1, 3, 4], between=(3, ": > .claude/agents/old.md"))
        self.assertIn("is not the reviewed content", out)

    def test_d1_step_6_commits_the_installed_files_and_removes_the_proposal(self):
        out = self.repo.run([0, 1, 3, 4, 6])
        self.assertNotIn("STOP:", out)
        self.assertIn("OK: step 6", out)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s").strip(), f"Install demo (reviewed {self.repo.c})")
        files = self.repo.git("show", "--no-renames", "--name-status", "--format=", "HEAD").split("\n")
        self.assertIn("M\t.claude/agents/old.md", files)
        self.assertIn("A\t.claude/scripts/new/tool.sh", files)
        self.assertIn(f"D\t{P}/README.md", files)
        self.assertFalse(self.repo.path(P).exists())
        self.assertEqual(self.repo.git("status", "--short"), "")

    def test_d2_step_6_verifies_again_and_commits_nothing_after_a_late_edit(self):
        head = self.repo.git("rev-parse", "HEAD")
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, "echo late >> .claude/agents/old.md"))
        self.assertIn("is not the reviewed content", out)
        self.assertNotIn("OK: step 6", out)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertTrue(self.repo.path(P).exists())

    def test_d3_step_6_stops_when_something_else_is_already_staged(self):
        head = self.repo.git("rev-parse", "HEAD")
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, "echo x > other.txt; git add other.txt"))
        self.assertIn("STOP:", out)
        self.assertNotIn("OK: step 6", out)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)

    def test_d4_a_failing_commit_is_a_stop_not_a_success(self):
        # an unwritable index lock makes git add fail: step 6 must say STOP and not claim success
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, "mkdir .git/index.lock"))
        self.assertIn("STOP:", out)
        self.assertNotIn("OK: step 6", out)

    def test_d5_step_6_does_not_say_restart_and_step_7_does_only_when_the_merge_is_done(self):
        out = self.repo.run([0, 1, 3, 4, 6])
        self.assertIn("OK: step 6", out)
        self.assertNotIn("restart", out.lower())
        self.assertIn("step 7", out)
        other = Repo()
        self.addCleanup(other.close)
        out = other.run([0, 1, 3, 4, 6, 7])
        self.assertIn("OK: step 7", out)
        self.assertGreater(out.lower().index("restart"), out.index("OK: step 7"))

    def test_b6_head_past_the_reviewed_commit_with_code_changes_stops_step_0(self):
        self.repo.write("tests/test_x.py", "print('evil')\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "after review")
        out = self.repo.run([0, 1, 3])
        self.assertIn("differs from the reviewed commit outside", out)
        self.assertNotIn("installed ", out)

    def test_b7_an_untracked_or_modified_file_outside_the_proposal_stops_step_0(self):
        for name, setup in {"untracked": lambda: self.repo.write("evil_test.py", "x\n"),
                            "modified": lambda: self.repo.write(".gitignore", "changed\n")}.items():
            with self.subTest(name):
                setup()
                out = self.repo.run([0, 1, 3])
                self.assertIn("outside the reviewed commit", out)
                self.assertNotIn("installed ", out)
                self.repo.path("evil_test.py").unlink(missing_ok=True)
                self.repo.write(".gitignore", "ignored.txt\n")

    def test_b8_backlog_and_reports_may_change_after_review(self):
        self.repo.write("docs/BACKLOG.md", "edited\n")
        self.repo.write("docs/reports/r.md", "report\n")
        out = self.repo.run([0, 1, 3, 4])
        self.assertIn("OK: installed files match", out)


class RollbackOnlyUndoesWhatWasInstalled(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def test_f1_a_new_target_that_already_existed_is_left_alone(self):
        self.repo.write(".claude/scripts/new/tool.sh", "squatter, not ours\n")
        out = self.repo.run([0, 1, 3, 5])
        self.assertIn("should be new but already exists", out)
        self.assertEqual(self.repo.path(".claude/scripts/new/tool.sh").read_text(), "squatter, not ours\n")
        self.assertIn("left alone", out)

    def test_f2_a_file_edited_by_hand_after_the_install_is_left_alone(self):
        out = self.repo.run([0, 1, 3, 5], between=(3, "echo mine >> .claude/agents/old.md"))
        self.assertIn("left alone", out)
        self.assertIn("mine", self.repo.path(".claude/agents/old.md").read_text())
        self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists(), "the installed new file is removed")

    def test_f3_the_rollback_with_nothing_installed_changes_nothing(self):
        out = self.repo.run([0, 5])
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertNotIn("STOP:", out)
        self.assertEqual(self.repo.git("status", "--short"), "")

    def test_f4_step_5_refuses_unsafe_map_lines(self):
        out = self.repo.run([0, 5], map_="0644 old.md ./.git/hooks/pre-commit")
        self.assertIn("STOP:", out)


class AllowedDocsNames(unittest.TestCase):
    """After review only docs/BACKLOG.md and docs/reports/<name>.md (no further /) may differ, committed or not."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def commit_file(self, rel):
        self.repo.write(rel, "x\n")
        self.repo.git("add", "-f", rel)
        self.repo.git("commit", "-q", "-m", "after review")

    def assertStops(self, out, *needles):
        self.assertIn("STOP:", out)
        self.assertNotIn("installed ", out)
        for needle in needles:
            self.assertIn(needle, out)

    def test_h1_untracked_conftest_in_reports_stops_it(self):
        self.repo.write("docs/reports/conftest.py", "print('evil')\n")
        self.assertStops(self.repo.run([0, 1, 3]), "docs/reports/conftest.py", "outside the reviewed commit")

    def test_h2_committed_conftest_in_reports_after_review_stops_it(self):
        self.commit_file("docs/reports/conftest.py")
        self.assertStops(self.repo.run([0, 1, 3]), "docs/reports/conftest.py", "differs from the reviewed commit outside")

    def test_h3_a_file_in_a_subfolder_of_reports_stops_it(self):
        self.repo.write("docs/reports/sub/x.md", "x\n")
        self.assertStops(self.repo.run([0, 1, 3]), "docs/reports/sub/x.md")
        self.repo.path("docs/reports/sub/x.md").unlink()
        self.commit_file("docs/reports/sub/y.md")
        self.assertStops(self.repo.run([0, 1, 3]), "docs/reports/sub/y.md")

    def test_h4_a_folder_named_backlog_stops_it(self):
        self.repo.write("docs/BACKLOG.md/x", "x\n")
        self.assertStops(self.repo.run([0, 1, 3]), "docs/BACKLOG.md/x")

    def test_h5_other_names_in_docs_or_reports_stop_it(self):
        for rel in ("docs/reports/x.py", "docs/reports/.md", "docs/other.md", "docs/BACKLOG.md.bak", "docs/reports/x.md.sh"):
            with self.subTest(rel):
                self.repo.write(rel, "x\n")
                self.assertStops(self.repo.run([0, 1, 3]), rel)
                self.repo.path(rel).unlink()

    def test_h6_a_staged_rename_into_a_bad_name_stops_it(self):
        self.repo.write("docs/reports/a.md", "a\n")
        self.repo.git("add", "docs/reports/a.md")
        self.repo.git("commit", "-q", "-m", "allowed report")
        self.repo.git("mv", "docs/reports/a.md", "docs/reports/conftest.py")
        out = self.repo.run([0, 1, 3])
        self.assertStops(out, "docs/reports/conftest.py")

    def test_h7_a_path_with_odd_characters_is_quoted_safely_and_stops_it(self):
        self.repo.write("docs/reports/we ird\\n.py", "x\n")
        out = self.repo.run([0, 1, 3])
        self.assertStops(out, "we")

    def test_h8_the_two_allowed_names_pass_committed_and_uncommitted(self):
        self.commit_file("docs/reports/2026-11-08-T-005.md")
        self.repo.write("docs/BACKLOG.md", "edited\n")
        self.repo.write("docs/reports/2026-11-09-T-006.md", "new\n")
        out = self.repo.run([0, 1, 3])
        self.assertNotIn("STOP:", out)
        self.assertIn("OK: step 3", out)


class Step6AndMerge(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def main_edit(self, rel, text):
        """Shell snippet that moves main by one commit editing rel, without touching the working tree or HEAD."""
        return (f"export GIT_INDEX_FILE=$PWD/.git/tmp-index; git read-tree main; "
                f"b=$(printf '{text}\\n' | git hash-object -w --stdin); git update-index --add --cacheinfo 100644,$b,{rel}; "
                "t=$(git write-tree); c=$(git commit-tree $t -p main -m moved); git update-ref refs/heads/main $c; "
                "unset GIT_INDEX_FILE; rm -f .git/tmp-index")

    def test_i1_happy_path_commits_then_merges_and_main_has_the_reviewed_blobs(self):
        out = self.repo.run([0, 1, 3, 4, 6, 7])
        self.assertNotIn("STOP:", out)
        self.assertIn("OK: step 6", out)
        self.assertIn("OK: step 7", out)
        self.assertEqual(self.repo.git("branch", "--show-current").strip(), "main")
        self.assertEqual(len(self.repo.git("log", "-1", "--format=%p").split()), 2, "a merge commit")
        for src, dst in (("old.md", ".claude/agents/old.md"), ("tool.sh", ".claude/scripts/new/tool.sh")):
            self.assertEqual(self.repo.git("rev-parse", f"main:{dst}"), self.repo.git("rev-parse", f"{self.repo.c}:{P}/{src}"))
        self.assertEqual(self.repo.git("ls-tree", "main", ".claude/scripts/new/tool.sh").split()[0], "100755")
        self.assertFalse(self.repo.path(P).exists())
        self.assertEqual(self.repo.git("status", "--short"), "")

    def test_i2_a_commit_on_the_branch_after_step_4_stops_step_6(self):
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, "echo x > late.txt; git -c core.hooksPath=/dev/null add late.txt; "
                                                         "git -c core.hooksPath=/dev/null commit -q -m late"))
        self.assertIn("STOP:", out)
        self.assertIn("HEAD", out)
        self.assertNotIn("OK: step 6", out)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s").strip(), "late")

    def test_i3_a_local_filter_added_after_step_4_stops_step_6_and_never_runs(self):
        marker = self.repo.tmp / "filter-marker"
        script = self.repo.tmp / "filter.sh"
        script.write_text(f"#!/bin/sh\necho ran >> {marker}\ncat\n")
        script.chmod(0o755)
        setup = f"echo '* filter=x' >> .git/info/attributes; git config filter.x.clean {script}"
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, setup))
        self.assertIn("STOP:", out)
        self.assertIn("filter.x.clean", out)
        self.assertNotIn("OK: step 6", out)
        self.assertFalse(marker.exists(), "the filter ran")
        # control: a bare git add would have run it
        self.repo.git("add", ".claude/agents/old.md")
        self.assertTrue(marker.exists(), "the test filter does not fire, so the test proves nothing")

    def test_i4_a_target_changed_on_main_after_step_1_stops_step_6_before_any_commit(self):
        head = self.repo.git("rev-parse", "HEAD")
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, self.main_edit(".claude/agents/old.md", "edited on main")))
        self.assertIn("STOP:", out)
        self.assertIn("changed on main since BASE", out)
        self.assertNotIn("OK: step 6", out)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)

    def test_i5_a_new_target_created_on_main_after_step_1_stops_step_6(self):
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, self.main_edit(".claude/scripts/new/tool.sh", "squat")))
        self.assertIn("exists on main", out)
        self.assertNotIn("OK: step 6", out)

    def test_i6_a_target_changed_on_main_between_step_6_and_7_stops_the_merge(self):
        out = self.repo.run([0, 1, 3, 4, 6, 7], between=(6, self.main_edit(".claude/agents/old.md", "edited on main")))
        self.assertIn("OK: step 6", out)
        self.assertIn("changed on main since BASE", out)
        self.assertNotIn("OK: step 7", out)
        self.assertEqual(self.repo.git("branch", "--show-current").strip(), BRANCH, "still on the task branch")
        self.assertEqual(len(self.repo.git("log", "-1", "--format=%p").split()), 1, "nothing merged")

    def test_i7_a_commit_after_step_6_stops_the_merge(self):
        out = self.repo.run([0, 1, 3, 4, 6, 7], between=(6, "echo x > late.txt; git -c core.hooksPath=/dev/null add late.txt; "
                                                            "git -c core.hooksPath=/dev/null commit -q -m late"))
        self.assertIn("STOP:", out)
        self.assertNotIn("OK: step 7", out)
        self.assertEqual(self.repo.git("branch", "--show-current").strip(), BRANCH)

    def test_i8_a_merge_conflict_is_a_stop_that_names_merge_abort(self):
        # main edits an unrelated file the branch also edits: not a target, so the pre-checks pass and the merge itself conflicts
        self.repo.write("shared.txt", "base\n")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("shared.txt", "base\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "shared")
        self.repo.git("checkout", "-q", BRANCH)
        self.repo.git("merge", "-q", "--no-edit", "main")
        c_before = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("checkout", "-q", "main")
        self.repo.write("shared.txt", "main side\n")
        self.repo.git("commit", "-q", "-am", "main side")
        self.repo.git("checkout", "-q", BRANCH)
        self.repo.write("shared.txt", "branch side\n")
        self.repo.git("commit", "-q", "-am", "branch side")
        reviewed = self.repo.git("rev-parse", "HEAD").strip()
        out = self.repo.run([0, 1, 3, 4, 6, 7], c=reviewed, base=self.repo.git("rev-parse", "main").strip())
        self.assertIn("STOP:", out)
        self.assertIn("merge --abort", out)
        self.assertNotIn("OK: step 7", out)

    def test_i9_a_subdirectory_is_a_stop(self):
        sub = self.repo.path("docs/sub")
        sub.mkdir(parents=True)
        out = self.repo.run([0, 1, 3], cwd=sub)
        self.assertIn("STOP:", out)
        self.assertIn("repository root", out)
        self.assertFalse(self.repo.path(".claude/scripts/new/tool.sh").exists())

    def test_i10_step_6_and_7_from_a_subdirectory_are_a_stop(self):
        self.repo.path("docs/sub").mkdir(parents=True)
        out = self.repo.run([0, 1, 3, 4, 6], between=(4, f"cd {self.repo.path('docs/sub')}"))
        self.assertIn("repository root", out)
        self.assertNotIn("OK: step 6", out)

    def test_i11_an_alias_named_g_does_not_break_the_blocks(self):
        prefix = "shopt -s expand_aliases\nalias g='git status'"
        self.repo.write(".claude/scripts/new/tool.sh", "squatter\n")
        out = self.repo.run([0, 1, 3], prefix=prefix)
        self.assertIn("should be new but already exists", out)
        self.assertNotIn("installed ", out)
        self.repo.path(".claude/scripts/new/tool.sh").unlink()
        out = self.repo.run([0, 1, 3, 4, 6, 7], prefix=prefix)
        self.assertNotIn("STOP:", out)
        self.assertIn("OK: step 7", out)

    def test_i12_the_rollback_counts_a_failed_restore_as_left_alone(self):
        out = self.repo.run([0, 1, 3, 5], between=(3, "touch .git/index.lock"))
        self.assertIn("could not be rolled back", out)
        self.assertNotIn("rolled back .claude/agents/old.md", out)
        self.assertNotIn("OK: step 5", out)
        self.assertIn("STOP:", out)

    def test_i13_the_rollback_counts_a_failed_remove_as_left_alone(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        d = self.repo.path(".claude/scripts/new")
        out = self.repo.run([0, 1, 3, 5], between=(3, f"chmod 555 {d}"))
        d.chmod(0o755)
        self.assertIn("could not be rolled back", out)
        self.assertNotIn("rolled back .claude/scripts/new/tool.sh", out)
        self.assertNotIn("OK: step 5", out)


class TempFilesAreRemoved(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def test_g1_step_2_removes_its_temp_directory_normally(self):
        self.repo.run([0, 1, 2])
        self.assertEqual(self.repo.leftovers(), [])

    def test_g2_step_2_removes_its_temp_directory_when_interrupted(self):
        self.repo.run([0, 1, 2], interrupt_step_2=True)
        self.assertEqual(self.repo.leftovers(), [])

    def test_g3_step_3_leaves_no_temp_file_after_a_successful_install(self):
        self.repo.run([0, 1, 3])
        self.assertEqual(self.repo.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
