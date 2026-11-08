#!/usr/bin/env python3
"""Scans a project for secrets and personal data before anything is pushed. Reports are included on purpose.

Usage: secret_scan.py [path ...]
  default: every file git tracks or would add (untracked, not ignored) under the current repo, so reports in
           docs/reports are covered; explicit paths scan only those files or folders.
Allow list: `secret_scan_allowlist` in .claude/project-profile.md (default .claude/secret-scan-allow.txt). One entry per
line, `<glob> <kind>[,<kind>...]`, for example `tests/**/*TokenTests.cs chat-bot-token`. It silences only those kinds in
only those files, so a real secret of another kind in the same file, or the same kind elsewhere, is still reported.
Exit codes: 0 clean, 1 findings, 2 could not scan (never read as a pass).
Output: file:line and the KIND of finding only. The matched text is never printed.
A line ending in `devteam-scan: ignore` is skipped (for documented fake examples).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_BYTES = 2_000_000
IGNORE_MARK = "devteam-scan: ignore"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("chat-bot-token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}")),
    ("push-monitor-url", re.compile(r"/api/push/[A-Za-z0-9]{8,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("api-key-header", re.compile(r"(?i)x-api-key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}")),
    ("chat-id", re.compile(r"(?i)chat[_ ]?id[\"'\s:=\[(]{0,6}-?\d{6,}")),
    ("secret-assignment", re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-+/=]{20,}")),
    ("cloud-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("email-address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]
EMAIL_ALLOWED = re.compile(r"(?i)^(noreply|no-reply)@|@(example\.(com|org|net)|users\.noreply\.github\.com)$")


def load_allowlist(root: Path) -> list[tuple[str, frozenset[str]]]:
    name = ".claude/secret-scan-allow.txt"
    try:
        profile = (root / ".claude" / "project-profile.md").read_text()
        match = re.search(r"^secret_scan_allowlist:\s*(\S+)\s*$", profile, re.M)
        name = match.group(1) if match else name
    except OSError:
        pass  # No profile: use the default path.
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        return []  # The allow list must live inside the project.
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:  # A glob with no kinds would silence a whole file: not accepted.
            entries.append((parts[0], frozenset(parts[1].split(","))))
    return entries


def allowed(rel: str, kind: str, allowlist: list[tuple[str, frozenset[str]]]) -> bool:
    return any(PurePosixPath(rel).full_match(glob) and kind in kinds for glob, kinds in allowlist)


def scan_text(text: str) -> list[tuple[int, str]]:
    """(line number, kind) for each finding. Never returns the matched text."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if IGNORE_MARK in line:
            continue
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if kind == "email-address" and EMAIL_ALLOWED.search(match.group(0)):
                    continue
                findings.append((number, kind))
    return findings


def git_root(start: str | None = None) -> Path:
    cmd = ["git", *(["-C", start] if start else []), "rev-parse", "--show-toplevel"]
    return Path(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip())


def git_files(root: Path) -> list[Path]:
    def run(*args: str) -> list[str]:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
        return [line for line in out.split("\0") if line]

    names = set(run("ls-files", "-z")) | set(run("ls-files", "-z", "--others", "--exclude-standard"))
    return sorted(root / n for n in names)


def findings_in(files: list[Path], root: Path | None) -> tuple[list[tuple[Path, int, str]], bool]:
    """(findings, complete). complete is False if a file could not be read: never treat that as clean."""
    allowlist = load_allowlist(root) if root else []
    found, complete = [], True
    for path in files:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            complete = False
            continue
        if b"\0" in data[:4096]:
            continue
        rel = path.relative_to(root).as_posix() if root and path.is_relative_to(root) else path.as_posix()
        for number, kind in scan_text(data.decode("utf-8", errors="replace")):
            if not allowed(rel, kind, allowlist):
                found.append((path, number, kind))
    return found, complete


def scan_repo(start: str) -> tuple[list[tuple[Path, int, str]], bool]:
    """The whole-repo scan used before every push. Raises if the repo cannot be listed."""
    root = git_root(start)
    return findings_in(git_files(root), root)


def expand(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        files.extend(sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p])
    return files


def main(argv: list[str]) -> int:
    try:
        paths = [a for a in argv[1:] if not a.startswith("--")]
        root = git_root()
        files = expand(paths) or git_files(root)
        found, complete = findings_in([f.resolve() for f in files], root.resolve())
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"secret_scan: could not list files ({type(error).__name__})", file=sys.stderr)
        return 2
    for path, number, kind in found:
        print(f"{path}:{number}: {kind}")
    if not complete:
        print("secret_scan: some files could not be read, so the scan is incomplete", file=sys.stderr)
        return 2
    if found:
        print(f"secret_scan: {len(found)} finding(s). Remove or redact them before pushing.", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
