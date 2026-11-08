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
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "PAGER": "cat", "LC_ALL": "C",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
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

    def run(self, steps, c=None, base=None, branch=BRANCH, map_=None, between=None, test_cmd="true"):
        """Run the filled-in blocks of the given steps in one bash process, like the boss's terminal."""
        step0 = block(0)
        values = {"C": c or self.c, "BASE": base or self.base, "B": branch, "P": P}
        for name, value in values.items():
            step0 = re.sub(rf"(?m)^{name}=.*$", lambda _m, n=name, v=value: f"{n}={v}", step0)
        step0 = re.sub(r"(?m)^MAP='.*'$", lambda _m: "MAP='" + (map_ or self.map) + "'", step0)
        pieces = []
        for n in steps:
            if n == 0:
                pieces.append(step0)
            elif n == 2:
                pieces.append(block(2).replace("| less", "| cat"))
            elif n == 4:
                pieces.append(re.sub(r"(?m)^env -u .*$", lambda _m: test_cmd, block(4)))
            else:
                pieces.append(block(n))
            if between and n == between[0]:
                pieces.append(between[1])
        done = subprocess.run(["bash", "-c", "\n".join(pieces)], cwd=self.dir, env=self.env, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

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
                out = self.repo.run([0, 3], map_=f"0644 {src} .claude/agents/x.md")
                self.assertStopsWithNothingInstalled(out)
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

    def test_b7_dangerous_targets_and_modes_stop_it(self):
        outside = self.repo.tmp / "outside.md"
        for line in ("0644 old.md ../escape.md", f"0644 old.md {outside}", "0644 old.md .git/hooks/pre-commit",
                     "0777 old.md .claude/agents/x.md", "4755 tool.sh .claude/agents/x.md", "0644 old.md a b c"):
            with self.subTest(line=line):
                out = self.repo.run([0, 3], map_=line)
                self.assertStopsWithNothingInstalled(out)
        self.assertFalse(outside.exists())
        self.assertFalse((self.repo.tmp / "escape.md").exists())
        self.assertFalse(self.repo.path(".git/hooks/pre-commit").exists())

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
        out = repo.run([0, 1, 3])
        self.assertIn("OK: step 3", out)
        self.assertEqual(repo.mode(".claude/agents/old.md"), 0o644)
        out = repo.run([0, 5])
        self.assertEqual(repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertTrue(repo.mode(".claude/agents/old.md") & 0o100, "the executable bit comes back")
        self.assertFalse(repo.path(".claude/scripts/new/tool.sh").exists())
        self.assertEqual(repo.git("status", "--short", "--ignored"), "")
        self.assertNotIn("STOP:", out)

    def test_b10b_rollback_after_an_install_that_stopped_part_way(self):
        # the second file cannot be installed because its parent directory is a file: step 3 stops after the first
        self.repo.write(".claude/scripts", "i am a file\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "squat")
        out = self.repo.run([0, 1, 3])
        self.assertIn("installed .claude/agents/old.md", out)
        self.assertIn("STOP: install stopped part-way; run step 5", out)
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "new content\n")
        self.repo.run([0, 5])
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertEqual(self.repo.git("status", "--short"), "")

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

    def test_b11b_the_test_repo_does_not_read_this_machines_git_config(self):
        self.assertEqual(self.repo.env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(self.repo.env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(self.repo.git("config", "--global", "--list"), "")


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

    def test_the_rollback_runs_no_hook_and_no_fsmonitor(self):
        out = self.repo.run([0, 1, 3, 5])
        self.assertEqual(self.repo.path(".claude/agents/old.md").read_text(), "old\n")
        self.assertFalse(self.marker.exists(), out)


if __name__ == "__main__":
    unittest.main()
