"""Tests for scripts/secret_scan.py. Fake secrets are assembled at runtime so this file is clean when scanned."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secret_scan.py"
sys.path.insert(0, str(SCRIPT.parent))

import secret_scan as s  # noqa: E402

TOKEN = "123456789" + ":" + "A" * 35
KINDS = lambda text: [kind for _, kind in s.scan_text(text)]  # noqa: E731


class Patterns(unittest.TestCase):
    def test_each_kind_is_found(self):
        cases = {
            "chat-bot-token": f"token {TOKEN}",
            "push-monitor-url": "http://kuma/api/" + "push/" + "abcdefghij",
            "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "api-key-header": "x-api-" + "key: " + "k" * 24,
            "chat-id": "chat" + "_id: " + "123456789",
            "secret-assignment": "api_" + "key = \"" + "a" * 24 + "\"",
            "cloud-access-key": "AKIA" + "A" * 16,
            "github-token": "ghp_" + "b" * 36,
            "email-address": "contact me at someone" + "@" + "mail.test",
        }
        for kind, text in cases.items():
            self.assertIn(kind, KINDS(text), kind)

    def test_ordinary_prose_and_code_are_clean(self):
        for text in ("The token budget is high.", "Use the API key from the secret store.", "chat IDs are never logged",
                     "x = compute(a, b)", "Co-Authored-By: Claude <noreply" + "@" + "anthropic.com>", "see user@example.com"):
            self.assertEqual(KINDS(text), [], text)

    def test_the_ignore_marker_skips_a_documented_fake(self):
        self.assertEqual(KINDS(f"example {TOKEN}  devteam-scan: ignore"), [])

    def test_the_matched_text_is_never_part_of_the_output(self):
        for _, kind in s.scan_text(f"token {TOKEN}"):
            self.assertNotIn(TOKEN, kind)


class Command(unittest.TestCase):
    def run_scan(self, *paths):
        return subprocess.run([sys.executable, str(SCRIPT), *paths], capture_output=True, text=True)

    def test_a_clean_folder_exits_0_and_a_dirty_one_exits_1_without_printing_the_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "2026-01-01-task.md"
            report.write_text("# Report\nAll fine.\n")
            self.assertEqual(self.run_scan(folder).returncode, 0)

            report.write_text(f"# Report\nthe bot token was {TOKEN}\n")
            result = self.run_scan(folder)

            self.assertEqual(result.returncode, 1)
            self.assertIn("chat-bot-token", result.stdout)
            self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_binary_and_unreadable_inputs_do_not_crash_it(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "blob.bin").write_bytes(b"\0\1\2" + TOKEN.encode())
            self.assertEqual(self.run_scan(folder).returncode, 0)

    def test_outside_a_git_repo_with_no_paths_it_errors_instead_of_passing(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=folder)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
