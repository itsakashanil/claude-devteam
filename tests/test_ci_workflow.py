"""Plain-text checks of the CI workflow (standard library only, no YAML parser).

These guard the rules in the T-020 plan: pinned actions, read-only token,
fixed triggers, no secrets, no script injection, every test suite is run.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS = ROOT / ".github" / "ci-requirements.txt"

SKIP_DIRS = {".git", ".team-work", "node_modules", "bin", "obj", "__pycache__"}


def read_workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def uses_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\s*(-\s+)?uses:", ln)]


def run_blocks(text: str) -> list[str]:
    """Return the text of every `run:` value (inline or block scalar)."""
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(-\s+)?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key_indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        value = m.group(3)
        body = [value]
        if value.strip() in ("|", ">", "|-", ">-", "|+", ">+"):
            body = []
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or indent_of(lines[j]) > key_indent):
                body.append(lines[j])
                j += 1
            i = j
        else:
            i += 1
        blocks.append("\n".join(body))
    return blocks


def job_sections(text: str) -> dict[str, str]:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^jobs:\s*$", ln))
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for ln in lines[start + 1 :]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", ln)
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(ln)
    return {name: "\n".join(body) for name, body in jobs.items()}


def step_text(lines: list[str], idx: int) -> str:
    """Text of the step that contains lines[idx] (a `uses:` line)."""
    m = re.match(r"^(\s*)-\s+", lines[idx])
    if m:
        start = idx
        item_indent = len(m.group(1))
    else:
        start = idx
        while start > 0 and not re.match(r"^\s*-\s+", lines[start]):
            start -= 1
        item_indent = indent_of(lines[start])
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() and indent_of(ln) <= item_indent:
            break
        end += 1
    return "\n".join(lines[start:end])


def test_folders() -> set[str]:
    folders: set[str] = set()
    for path in ROOT.rglob("test_*.py"):
        rel = path.relative_to(ROOT)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        folders.add(rel.parent.as_posix())
    return folders


class WorkflowFileTests(unittest.TestCase):
    def test_workflow_exists(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), f"missing {WORKFLOW}")

    def test_every_uses_is_pinned_to_a_full_sha_with_tag_comment(self) -> None:
        found = uses_lines(read_workflow())
        self.assertTrue(found, "no uses: lines found")
        pattern = re.compile(
            r"^\s*(-\s+)?uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}\s+#\s*v\d+(\.\d+)+\s*$"
        )
        for ln in found:
            self.assertRegex(ln, pattern, f"bad pin: {ln.strip()}")

    def test_permissions_top_level_contents_read_only(self) -> None:
        lines = read_workflow().splitlines()
        idx = [i for i, ln in enumerate(lines) if re.match(r"^permissions:", ln)]
        self.assertEqual(len(idx), 1, "need exactly one top-level permissions:")
        i = idx[0]
        self.assertRegex(lines[i], r"^permissions:\s*$")
        body = []
        for ln in lines[i + 1 :]:
            if ln.strip() and indent_of(ln) == 0:
                break
            if ln.strip():
                body.append(ln.strip())
        self.assertEqual(body, ["contents: read"])

    def test_no_per_job_permissions(self) -> None:
        for name, body in job_sections(read_workflow()).items():
            self.assertNotRegex(body, r"(?m)^\s+permissions:", f"job {name} sets permissions")

    def test_triggers_are_pull_request_and_push_to_main_only(self) -> None:
        text = read_workflow()
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("workflow_run", text)
        lines = text.splitlines()
        i = next(i for i, ln in enumerate(lines) if re.match(r"^(on|\"on\"|'on'):\s*$", ln))
        block = []
        for ln in lines[i + 1 :]:
            if ln.strip() and indent_of(ln) == 0:
                break
            block.append(ln)
        keys = [m.group(1) for ln in block if (m := re.match(r"^  ([A-Za-z_]+):", ln))]
        self.assertEqual(sorted(keys), ["pull_request", "push"])
        joined = "\n".join(block)
        inline = r"push:\s*\n\s+branches:\s*\[\s*main\s*\]"
        listed = r"push:\s*\n\s+branches:\s*\n\s+-\s+main\s*(\n|$)"
        self.assertRegex(joined, f"(?s){inline}|{listed}")

    def test_no_secrets_reference(self) -> None:
        self.assertNotIn("secrets.", read_workflow())

    def test_no_event_fields_inside_run(self) -> None:
        for block in run_blocks(read_workflow()):
            self.assertNotIn("github.event", block)
            self.assertNotIn("github.head_ref", block)
            self.assertNotIn("${{", block, "no expression inside run:, use env")

    def test_every_job_has_timeout_and_fixed_runner(self) -> None:
        jobs = job_sections(read_workflow())
        self.assertTrue(jobs, "no jobs found")
        for name, body in jobs.items():
            self.assertRegex(body, r"(?m)^    timeout-minutes:\s*\d+\s*$", f"{name}: timeout")
            self.assertRegex(body, r"(?m)^    runs-on:\s*ubuntu-24\.04\s*$", f"{name}: runs-on")

    def test_every_checkout_does_not_persist_credentials(self) -> None:
        lines = read_workflow().splitlines()
        checkouts = [i for i, ln in enumerate(lines) if "uses: actions/checkout@" in ln]
        self.assertTrue(checkouts, "no checkout step")
        for i in checkouts:
            self.assertRegex(step_text(lines, i), r"(?m)^\s+persist-credentials:\s*false\s*$")

    def test_every_test_folder_is_run_by_the_workflow(self) -> None:
        folders = test_folders()
        self.assertTrue(folders, "no test folders found")
        discover = [ln for ln in read_workflow().splitlines() if "unittest" in ln or "pytest" in ln]
        text = "\n".join(discover)
        for folder in sorted(folders):
            self.assertRegex(
                text,
                r"(^|[\s'\"])\.?/?" + re.escape(folder) + r"/?([\s'\"]|$)",
                f"workflow does not run tests in {folder}",
            )

    def test_the_plugin_workflow_has_no_dotnet_and_runs_the_scan_and_ruff(self) -> None:
        text = read_workflow()
        self.assertNotIn("dotnet", text)
        self.assertIn("python3 scripts/secret_scan.py", text)
        self.assertIn("ruff check", text)
        self.assertIn("ruff format --check", text)


class RequirementsTests(unittest.TestCase):
    def test_requirement_lines_carry_hashes(self) -> None:
        self.assertTrue(REQUIREMENTS.is_file(), f"missing {REQUIREMENTS}")
        lines = [
            ln
            for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.assertTrue(lines)
        for ln in lines:
            self.assertRegex(ln, r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*\s+--hash=sha256:[0-9a-f]{64}\s*$")


if __name__ == "__main__":
    unittest.main()
