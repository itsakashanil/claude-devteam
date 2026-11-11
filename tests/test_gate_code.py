"""Tests for scripts/gate-code. Run: python3 -m unittest discover -s tests"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "gate-code"
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE = re.compile(f"^[{ALPHABET}]{{6}}$")


class GateCode(unittest.TestCase):
    """T-011b: gate-code prints one fresh one-time code, takes no input and touches nothing."""

    def run_gate(self, *args, python_flags=(), script=GATE, cwd=None):
        return subprocess.run([sys.executable, *python_flags, str(script), *args], capture_output=True, text=True, timeout=30, cwd=cwd)

    def plant(self, prefix):
        folder = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, folder, True)
        marker = folder / "planted-ran"
        (folder / "secrets.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nchoice = None\n")
        copy = folder / "gate-code"
        copy.write_text(GATE.read_text())
        copy.chmod(0o755)
        return folder, marker, copy

    def test_it_prints_exactly_one_line_of_six_characters_and_exits_0(self):
        result = subprocess.run([str(GATE)], capture_output=True, text=True, timeout=30)  # Through the shebang, as the manager runs it.

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertRegex(result.stdout, f"^[{ALPHABET}]{{6}}\n$")

    def test_two_hundred_runs_all_match_and_are_nearly_all_different(self):
        codes = [self.run_gate(python_flags=("-I",)).stdout.strip() for _ in range(200)]

        self.assertTrue(all(CODE.match(code) for code in codes))
        self.assertGreaterEqual(len(set(codes)), 190)

    def test_any_argument_is_refused_with_usage_and_no_code(self):
        for args in (("x",), ("--help",), ("-c", "print(1)"), ("a", "b")):
            result = self.run_gate(*args, python_flags=("-I",))
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, "", args)
            self.assertIn("usage", result.stderr.lower(), args)

    def test_without_isolated_mode_it_refuses_and_prints_no_code(self):
        result = self.run_gate()

        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")

    def test_a_planted_secrets_module_next_to_it_is_not_imported(self):
        folder, marker, copy = self.plant("gate-plant-")

        result = self.run_gate(python_flags=("-I",), script=copy, cwd=folder)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), CODE)
        self.assertFalse(marker.exists(), "the planted secrets.py was imported")

    def test_run_through_the_shebang_a_planted_secrets_in_its_folder_and_cwd_is_not_imported(self):
        folder, marker, copy = self.plant("gate-shebang-")

        result = subprocess.run([str(copy)], capture_output=True, text=True, timeout=30, cwd=folder)  # The shebang alone.

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), CODE)
        self.assertFalse(marker.exists(), "the planted secrets.py was imported")

    def test_it_creates_no_file_and_reads_nothing_from_its_directory(self):
        folder = Path(tempfile.mkdtemp(prefix="gate-empty-"))
        self.addCleanup(shutil.rmtree, folder, True)

        result = self.run_gate(python_flags=("-I",), cwd=folder)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(folder.iterdir()), [])

    def test_the_source_has_no_network_file_or_process_calls(self):
        source = GATE.read_text()
        for banned in ("socket", "urllib", "open(", "subprocess", "os.system", "http"):
            self.assertNotIn(banned, source, banned)

    def test_the_shebang_isolates_python_and_the_file_is_executable_0755(self):
        self.assertEqual(GATE.read_text().splitlines()[0], "#!/usr/bin/python3 -I")
        self.assertTrue(os.access("/usr/bin/python3", os.X_OK))
        self.assertEqual(stat.S_IMODE(GATE.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
