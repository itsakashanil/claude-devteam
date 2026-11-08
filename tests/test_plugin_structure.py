"""Checks the plugin's structure and that its generic parts stay generic. Run: python3 -m unittest discover -s tests"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = {p.stem: p.read_text() for p in (ROOT / "agents").glob("*.md")}


def frontmatter(text):
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "missing frontmatter"
    return dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", match.group(1), re.M))


class Manifest(unittest.TestCase):
    def test_manifest_and_marketplace_are_valid_and_consistent(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(plugin["name"], "devteam")
        self.assertRegex(plugin["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertEqual(market["plugins"][0]["source"], "./")

    def test_the_hook_runs_the_guard_and_blocks_if_it_fails(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(hooks[0]["matcher"], "Bash")
        hook = hooks[0]["hooks"][0]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/guard_bash.py", hook["command"])
        self.assertEqual(hook["onFailure"], "block")
        self.assertTrue((ROOT / "scripts" / "guard_bash.py").stat().st_mode & 0o111)

    def test_the_three_commands_exist_and_do_not_run_on_their_own(self):
        for name in ("init", "go", "status"):
            fields = frontmatter((ROOT / "skills" / name / "SKILL.md").read_text())
            self.assertGreater(len(fields["description"]), 30, name)
            self.assertEqual(fields.get("disable-model-invocation"), "true", name)
        self.assertIn("$ARGUMENTS", (ROOT / "skills" / "go" / "SKILL.md").read_text())
        self.assertIn("argument-hint", (ROOT / "skills" / "go" / "SKILL.md").read_text())


class Agents(unittest.TestCase):
    def test_the_team_is_exactly_these_six_with_the_right_models(self):
        models = {n: frontmatter(t)["model"] for n, t in AGENTS.items()}
        self.assertEqual(models, {"manager": "opus", "planner": "opus", "reviewer": "opus", "builder": "sonnet",
                                  "tester": "sonnet", "deployer": "sonnet"})

    def test_names_match_files(self):
        for name, text in AGENTS.items():
            self.assertEqual(frontmatter(text)["name"], name)

    def test_plugin_agents_use_only_fields_plugins_support(self):
        for name, text in AGENTS.items():
            for banned in ("permissionMode", "hooks", "mcpServers", "initialPrompt"):
                self.assertNotIn(banned + ":", text.split("---")[1], f"{name} uses {banned}, which plugin agents ignore")

    def test_reviewer_and_planner_are_read_only(self):
        self.assertEqual(sorted(re.split(r",\s*", frontmatter(AGENTS["reviewer"])["tools"])), ["Glob", "Grep", "Read"])
        self.assertEqual(sorted(re.split(r",\s*", frontmatter(AGENTS["planner"])["tools"])), ["Glob", "Grep", "Read"])
        for banned in ("Edit", "Write", "Bash"):
            self.assertIn(banned, frontmatter(AGENTS["reviewer"])["disallowedTools"])

    def test_only_the_manager_can_start_agents(self):
        self.assertIn("AskUserQuestion", frontmatter(AGENTS["manager"])["tools"])
        for name, text in AGENTS.items():
            self.assertEqual("Agent(" in frontmatter(text)["tools"], name == "manager", name)

    def test_every_agent_reads_the_profile_treats_outside_content_as_data_and_stops_when_denied(self):
        for name, text in AGENTS.items():
            self.assertIn(".claude/project-profile.md", text, name)
            self.assertRegex(text, r"(?i)as data", name)
            self.assertIn("## When something is denied", text, name)
            self.assertIn("do not rewrite it as a script", text, name)

    def test_the_manager_carries_the_boss_rules_and_the_report_step(self):
        text = AGENTS["manager"]
        for phrase in ("Approve a gate for the boss", "Overrule a failed review", "Change CLAUDE.md", "3 times in a row",
                       "At most 2 per task", "docs/BACKLOG.md", "docs/reports/YYYY-MM-DD-<task>.md", "INDEX.md",
                       "secret_scan.py", "learning notes", "never push to the remote unless the boss asks", '"status"',
                       "waiting for the boss to deploy"):
            self.assertIn(phrase, text, phrase)

    def test_the_deployer_never_deploys_by_default_and_links_the_report(self):
        text = AGENTS["deployer"]
        self.assertIn("ready to deploy <version>", text)
        self.assertIn("the report link", text)
        self.assertIn("MISMATCH", text)
        self.assertIn("never run sudo", text.lower())


class Generic(unittest.TestCase):
    PROJECT_WORDS = ("homelab", "immich", "telegram", "akash", "arya", "botcore", "/etc/homelab")

    def test_nothing_project_specific_leaks_into_the_generic_parts(self):
        scanned = [p for folder in ("agents", "skills", "templates", "scripts", "hooks") for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        for path in scanned:
            text = path.read_text().lower()
            for word in self.PROJECT_WORDS:
                self.assertNotIn(word, text, f"{path.relative_to(ROOT)} mentions {word}")

    def test_the_report_template_has_every_section_the_boss_asked_for(self):
        text = (ROOT / "templates" / "report.md").read_text()
        for heading in ("## Goal", "## Plan", "## Files", "## Decisions and rejected alternatives", "## Tests", "## Review",
                        "## Security gates", "## Deploy result and rollback point", "## Open items", "## Learning notes"):
            self.assertIn(heading, text)

    def test_the_profile_template_has_a_parseable_machine_block(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import guard_bash

        text = (ROOT / "templates" / "project-profile.md").read_text()
        block = re.search(r"```devteam-profile\n(.*?)```", text, re.S).group(1)
        keys = {line.split(":", 1)[0] for line in block.splitlines() if ":" in line}
        self.assertEqual(keys, {"project_name", "repo_url", "deploy_command_path", "build_output_dir", "secret_locations", "protected_paths_file"})
        self.assertTrue(callable(guard_bash.read_profile))

    def test_no_template_holds_a_secret_shaped_string(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import secret_scan

        for path in (ROOT / "templates").rglob("*"):
            if path.is_file():
                self.assertEqual(secret_scan.scan_text(path.read_text()), [], path.name)


if __name__ == "__main__":
    unittest.main()
