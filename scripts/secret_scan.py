#!/usr/bin/env python3
"""Scans a project for secrets and personal data before anything is pushed. Reports are included on purpose.

Usage: secret_scan.py [--all] [path ...]
  default: every file git tracks or would add (untracked, not ignored) under the current repo, so reports in
           docs/reports are covered; explicit paths scan only those files or folders.
Exit codes: 0 clean, 1 findings, 2 could not scan (never read as a pass).
Output: file:line and the KIND of finding only. The matched text is never printed.
A line ending in `devteam-scan: ignore` is skipped (for documented fake examples).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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


def git_files() -> list[Path]:
    def run(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
        return [line for line in out.split("\0") if line]

    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    names = set(run("ls-files", "-z")) | set(run("ls-files", "-z", "--others", "--exclude-standard"))
    return sorted(root / n for n in names)


def expand(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        files.extend(sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p])
    return files


def main(argv: list[str]) -> int:
    try:
        files = expand([a for a in argv[1:] if not a.startswith("--")]) or git_files()
    except (subprocess.CalledProcessError, OSError) as error:
        print(f"secret_scan: could not list files ({type(error).__name__})", file=sys.stderr)
        return 2

    found = 0
    for path in files:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            print(f"secret_scan: could not read {path}", file=sys.stderr)
            return 2
        if b"\0" in data[:4096]:
            continue
        for number, kind in scan_text(data.decode("utf-8", errors="replace")):
            print(f"{path}:{number}: {kind}")
            found += 1
    if found:
        print(f"secret_scan: {found} finding(s). Remove or redact them before pushing.", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
