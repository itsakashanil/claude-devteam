#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool: the rules a permission file cannot express. Generic: it knows nothing
about any one project. Project specifics come from .claude/project-profile.md (see templates/project-profile.md).

Exit 2 blocks the call and the reason on stderr goes back to the agent. Anything unparseable is blocked.
A JSON "ask" decision on stdout (exit 0) makes the permission prompt appear for the boss (gate 3).

  1. sudo and other privilege tools: always blocked, for every agent. Deploys are run by the boss.
  2. rm, rmdir, unlink, shred, find -delete/-exec: only inside the repo, the build output directory and the
     session scratchpad, and never the repo root or .git itself.
  3. Writing to the team's own rules: cp, mv, install, ln, tee, sed -i, touch, chmod, rm... aimed at anything
     under .claude/ is blocked. The same commands aimed at a protected path (the file named by
     `protected_paths_file`) ask the boss first.
  4. git push: no force, delete or mirror pushes in any spelling, and a plain push runs the full-repo secret scan
     first (secret_scan.py, with the project's allow list for test fixtures) and is blocked on any finding or error.
     A push that passes both checks still asks the boss in the permission prompt, for every agent.
  5. The project's secret locations (`secret_locations` in the profile) are never mentioned.

It looks inside bash -c, env, timeout, xargs, find -exec, heredocs and command substitutions (including ones
inside double quotes), tracks cd, and blocks anything it cannot parse.

With no profile the strictest generic rules still apply: deletes only inside the repo, no force pushes.

This is a guardrail against mistakes and prompt injection, not a sandbox: a program that builds its own command
line (python -c, a script file) is beyond what a text check can see. The real boundary is OS account separation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEPLOYER = "deployer"
MAX_DEPTH = 6
PROFILE = ".claude/project-profile.md"

PUNCTUATION = "();<>|&\n"
SEPARATOR_CHARS = set(";&|()\n")  # A token made only of these ends a command; < and > are redirections.
SHELL_KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PRIVILEGE_TOOLS = {"su", "doas", "pkexec", "run0", "sudoedit", "chroot", "nsenter", "unshare"}
DELETERS = {"rm", "rmdir", "unlink", "shred"}
WRITERS = DELETERS | {"cp", "mv", "install", "ln", "rsync", "tee", "truncate", "dd", "touch", "chmod", "chown", "mkdir", "patch"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
GLOB_CHARS = set("*?[")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")


@dataclass(frozen=True)
class Context:
    repo: str
    cwd: str
    agent_type: str | None
    scratchpad: str | None
    publish_dir: str | None = None
    protected: tuple[str, ...] = ()
    secret_paths: tuple[str, ...] = ()  # Locations nobody on the team may mention.


@dataclass(frozen=True)
class Verdict:
    deny: str | None = None
    ask: str | None = None


class Unparseable(Exception):
    pass


# --- reading the command ---------------------------------------------------------------------------

def tokenize(command: str) -> list[list[str]]:
    """Splits into simple commands at ; & | ( ) and newlines that are outside quotes. Quoted text is left exactly as
    written, so a script passed to `bash -c "..."` keeps its own newlines for the recursive check."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"  # Not the newline: that is a separator.
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise Unparseable(str(error)) from error

    commands: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= SEPARATOR_CHARS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [c for c in commands if c]


def split_heredocs(command: str) -> tuple[str, list[str]]:
    """Removes heredoc bodies (they are input data, not commands) and returns them with the substitutions a
    body would still run: an unquoted delimiter expands $( ) and backticks inside the body."""
    lines = command.split("\n")
    kept: list[str] = []
    scripts: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC.finditer(line):
            quote, tag = match.group(1), match.group(2)
            dash = match.group(0).startswith("<<-")
            end = next(
                (j for j in range(index, len(lines)) if (lines[j].lstrip("\t") if dash else lines[j]) == tag),
                None,
            )
            if end is None:
                continue  # No closing line, so this was not a heredoc (for example `x << 2` in an expression).
            if not quote:
                scripts.extend(substitutions("\n".join(lines[index:end])))
            index = end + 1
    return "\n".join(kept), scripts


def substitutions(text: str) -> list[str]:
    """The text of every $( ... ) and `...` outside single quotes, including inside double quotes."""
    found: list[str] = []
    index, in_single, in_double = 0, False, False
    while index < len(text):
        char = text[index]
        if char == "\\" and not in_single:
            index += 2
        elif in_single:
            in_single = char != "'"
            index += 1
        elif char == "'" and not in_double:  # An apostrophe inside double quotes is just a letter.
            in_single = True
            index += 1
        elif char == '"':
            in_double = not in_double
            index += 1
        elif text.startswith("$(", index):
            end = matching_paren(text, index + 1)
            found.append(text[index + 2 : end])
            index = end + 1
        elif char == "`":
            end = text.find("`", index + 1)
            if end == -1:
                raise Unparseable("unclosed backtick")
            found.append(text[index + 1 : end])
            index = end + 1
        else:
            index += 1
    return found


def matching_paren(text: str, open_index: int) -> int:
    depth, index, in_single, in_double = 0, open_index, False, False
    while index < len(text):
        char = text[index]
        if char == "\\" and not in_single:
            index += 1
        elif in_single:
            in_single = char != "'"
        elif char == "'" and not in_double:
            in_single = True
        elif char == '"':
            in_double = not in_double
        elif in_double:
            pass  # Parentheses inside a quoted string do not nest.
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise Unparseable("unclosed $(")


def strip_prefixes(tokens: list[str]) -> list[str]:
    """Drops leading shell keywords and VAR=value assignments."""
    index = 0
    while index < len(tokens) and (tokens[index] in SHELL_KEYWORDS or re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[index])):
        index += 1
    return tokens[index:]


def unwrap(tokens: list[str]) -> list[str]:
    """Finds the command that really runs behind env, timeout, nohup and friends."""
    tokens = strip_prefixes(tokens)
    while tokens:
        program = os.path.basename(tokens[0])
        if program in {"nohup", "command", "builtin", "exec", "setsid", "time", "stdbuf"} or program == "env":
            tokens = strip_prefixes(skip_options(tokens[1:], with_value={"-u", "-C", "-S", "-i", "-o", "-e"}))
        elif program == "nice" or program == "ionice":
            tokens = strip_prefixes(skip_options(tokens[1:], with_value={"-n", "-c"}))
        elif program == "timeout":
            rest = skip_options(tokens[1:], with_value={"-s", "-k", "--signal", "--kill-after"})
            tokens = strip_prefixes(rest[1:])  # The first positional is the duration.
        elif program == "flock":
            rest = skip_options(tokens[1:], with_value={"-w", "-E"})
            tokens = strip_prefixes(rest[1:])  # The first positional is the lock file.
        elif program == "watch":
            tokens = strip_prefixes(skip_options(tokens[1:], with_value={"-n", "-d"}))
        else:
            break
    return tokens


def skip_options(tokens: list[str], with_value: set[str]) -> list[str]:
    index = 0
    while index < len(tokens) and tokens[index].startswith("-") and tokens[index] != "--":
        index += 2 if tokens[index] in with_value else 1
    return tokens[index:]


# --- the verdict -----------------------------------------------------------------------------------

def secret_pattern(ctx: Context) -> re.Pattern | None:
    paths = [p.rstrip("/") for p in ctx.secret_paths if p.strip("/ ")]
    return re.compile("|".join(re.escape(p) + r"(?![\w-])" for p in paths)) if paths else None


def evaluate_full(command: str, ctx: Context) -> Verdict:
    pattern = secret_pattern(ctx)
    if pattern and pattern.search(command):
        return Verdict(deny="the project's secret locations (Secret locations in .claude/project-profile.md) are off limits to the dev team")

    state = {"cwd": ctx.cwd, "asks": [], "depth": 0}
    try:
        body, hidden = split_heredocs(command)
        for script in [body, *hidden]:
            reason = check_script(script, ctx, state)
            if reason:
                return Verdict(deny=reason)
    except Unparseable:
        return Verdict(deny="the command could not be parsed safely (unbalanced quotes or brackets?); rewrite it more simply")

    return Verdict(ask=state["asks"][0] if state["asks"] else None)


def evaluate(command: str, ctx: Context) -> str | None:
    """The reason to block the command, or None to let it through."""
    return evaluate_full(command, ctx).deny


def evaluate_ask(command: str, ctx: Context) -> str | None:
    """The reason the boss must be asked first (only meaningful when evaluate() returns None)."""
    return evaluate_full(command, ctx).ask


def check_script(script: str, ctx: Context, state: dict) -> str | None:
    if state["depth"] > MAX_DEPTH:
        return "commands nested too deeply to check"

    inner_state = {"cwd": state["cwd"], "asks": state["asks"], "depth": state["depth"] + 1}
    for inner in substitutions(script):
        reason = check_script(inner, ctx, dict(inner_state))
        if reason:
            return reason

    return check_commands(tokenize(script), ctx, state)


def check_commands(commands: list[list[str]], ctx: Context, state: dict) -> str | None:
    for tokens in commands:
        reason = check_one(unwrap(tokens), ctx, state)
        if reason:
            return reason
    return None


def check_one(tokens: list[str], ctx: Context, state: dict) -> str | None:
    if not tokens:
        return None

    program, args = os.path.basename(tokens[0]), tokens[1:]

    if program == "sudo" or program in PRIVILEGE_TOOLS:
        return check_sudo(tokens, ctx)

    if program in {"cd", "pushd"}:
        state["cwd"] = next_directory(args, state["cwd"])
        return None

    if program in SHELLS or program == "eval":
        return check_inline_script(program, args, ctx, state)

    if program == "xargs":
        return check_xargs(args, ctx, state)

    if program == "find":
        return check_find(args, ctx, state)

    if program in DELETERS:
        reason = check_delete(program, args, ctx, state["cwd"])
        if reason:
            return reason

    if program in WRITERS or (program == "sed" and any(re.fullmatch(r"-[A-Za-z]*i.*|--in-place.*", a) for a in args)):
        return check_writes(program, args, ctx, state)

    if program == "git":
        return check_git(args, ctx, state)

    return None


# --- sudo ------------------------------------------------------------------------------------------

def check_sudo(tokens: list[str], ctx: Context) -> str | None:
    args = tokens[1:]
    if args[:1] in (["-n"], ["--non-interactive"]):
        args = args[1:]

    return "sudo and other privilege tools are blocked for every agent; the boss runs deploys and anything else that needs root"


# --- shells, xargs, eval ---------------------------------------------------------------------------

def check_inline_script(program: str, args: list[str], ctx: Context, state: dict) -> str | None:
    if program == "eval":
        script = " ".join(args)
    else:
        script = None
        for index, arg in enumerate(args):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:] and index + 1 < len(args):
                script = args[index + 1]
                break
        if script is None:
            return None  # A shell running a file or reading stdin: nothing here to inspect.

    try:
        body, hidden = split_heredocs(script)
        for text in [body, *hidden]:
            reason = check_script(text, ctx, dict(state, depth=state["depth"] + 1))
            if reason:
                return reason
    except Unparseable:
        return "the script passed to the shell could not be parsed safely"
    return None


def check_xargs(args: list[str], ctx: Context, state: dict) -> str | None:
    inner = unwrap(skip_options(args, with_value={"-I", "-n", "-L", "-P", "-s", "-d", "-E", "-a"}))
    if inner and os.path.basename(inner[0]) in DELETERS:
        return "xargs with a delete command takes its paths from stdin, which cannot be checked; name the paths instead"
    return check_one(inner, ctx, state) if inner else None


# --- find ------------------------------------------------------------------------------------------

def check_find(args: list[str], ctx: Context, state: dict) -> str | None:
    acting = any(arg in FIND_ACTIONS for arg in args)

    if acting:
        starts = []
        for arg in args:
            if arg.startswith("-") or arg in {"!", "(", ")"}:
                break
            starts.append(arg)
        starts = starts or ["."]
        stray = [arg for arg in args[len(starts):] if arg.startswith(("/", "~"))]
        for path in starts + stray:
            if not allowed_target(path, ctx, state["cwd"], root_ok=True):
                return f"find with -delete or -exec may only search inside the repo, not {path!r}"
            reason = control_check(path, ctx, state, searching=True)
            if reason:
                return reason

    for index, arg in enumerate(args):
        if arg in {"-exec", "-execdir", "-ok", "-okdir"}:
            inner = [a for a in args[index + 1:] if a not in {"+"}]
            reason = check_one(unwrap(inner), ctx, state)
            if reason:
                return reason
    return None


# --- deleting --------------------------------------------------------------------------------------

def path_arguments(args: list[str]) -> list[str]:
    paths, options_done = [], False
    for arg in args:
        if not options_done and arg == "--":
            options_done = True
        elif not options_done and arg.startswith("-"):
            continue
        else:
            paths.append(arg)
    return paths


def check_delete(program: str, args: list[str], ctx: Context, cwd: str | None) -> str | None:
    for path in path_arguments(args):
        if not allowed_target(path, ctx, cwd):
            return (
                f"{program} may only remove things inside the repo, the build output or the scratchpad, "
                f"never the repo root or .git; refusing {path!r}"
            )
    return None


def allowed_target(path: str, ctx: Context, cwd: str | None, root_ok: bool = False) -> bool:
    """root_ok: the repo root itself is acceptable (as a place to search from, never as something to delete)."""
    if "$" in path or "`" in path:
        return False  # The shell would decide what this means, and we cannot.

    if path.startswith("~"):
        path = os.path.expanduser(path)
    elif not path.startswith("/"):
        if cwd is None:
            return False
        path = os.path.join(cwd, path)

    repo = os.path.realpath(ctx.repo)
    globbed = [i for i, part in enumerate(path.split("/")) if GLOB_CHARS & set(part)]
    if globbed:
        first = path.split("/")[globbed[0]]
        prefix = "/".join(path.split("/")[: globbed[0]]) or "/"
        resolved_prefix = resolve(prefix)
        if resolved_prefix == repo and first:
            return False  # A wildcard straight in the repo root would sweep up the whole tree.
        return is_inside(resolved_prefix, ctx, repo, prefix_only=True)

    return is_inside(resolve(path), ctx, repo, prefix_only=root_ok)


def resolve(path: str) -> str:
    """Normalises the path and resolves symlinks in its directory, but not the final name: removing a link removes the link."""
    absolute = os.path.normpath(path)
    parent, name = os.path.split(absolute)
    return os.path.join(os.path.realpath(parent), name) if name else absolute


def is_inside(path: str, ctx: Context, repo: str, prefix_only: bool = False) -> bool:
    if path == repo:
        return prefix_only  # A wildcard below the repo root is fine; the repo root itself never is.
    if path == repo + "/.git":
        return False
    if path.startswith(repo + "/"):
        return True

    if ctx.publish_dir:
        publish = os.path.realpath(ctx.publish_dir)
        if path == publish or path.startswith(publish + "/"):
            return True

    if ctx.scratchpad:
        scratch = os.path.realpath(ctx.scratchpad)
        if path.startswith(scratch + "/"):
            return True
    return False


def next_directory(args: list[str], cwd: str | None) -> str | None:
    targets = [a for a in args if not a.startswith("-") or a == "-"]
    if not targets:
        return os.path.expanduser("~")
    target = targets[0]
    if target == "-" or "$" in target or GLOB_CHARS & set(target):
        return None
    if target.startswith("~"):
        return os.path.normpath(os.path.expanduser(target))
    if target.startswith("/"):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(cwd, target)) if cwd else None


# --- writing to the team's own rules and to protected code ------------------------------------------

def check_writes(program: str, args: list[str], ctx: Context, state: dict) -> str | None:
    targets = [a[3:] for a in args if a.startswith("of=")] if program == "dd" else path_arguments(args)
    for target in targets:
        reason = control_check(target, ctx, state)
        if reason:
            return reason
    return None


def repo_relative(path: str, ctx: Context, cwd: str | None) -> str | None:
    """The path relative to the repo root, or None if it is outside the repo or cannot be worked out."""
    if "$" in path or "`" in path:
        return None
    if path.startswith("~"):
        path = os.path.expanduser(path)
    elif not path.startswith("/"):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)

    parts = path.split("/")
    cut = next((i for i, part in enumerate(parts) if GLOB_CHARS & set(part)), None)
    if cut is not None:
        path = "/".join(parts[:cut]) or "/"

    resolved = resolve(path)
    repo = os.path.realpath(ctx.repo)
    if resolved == repo:
        return ""
    return resolved[len(repo) + 1:] if resolved.startswith(repo + "/") else None


def control_check(path: str, ctx: Context, state: dict, searching: bool = False) -> str | None:
    """Deny anything under .claude/; ask the boss about protected paths."""
    relative = repo_relative(path, ctx, state["cwd"])
    if relative is None:
        return None

    if relative == ".claude" or relative.startswith(".claude/"):
        return "the team's own rules under .claude/ cannot be changed from the shell; only the boss edits them"

    if not searching and is_protected(relative, ctx.protected):
        state["asks"].append(f"{relative} is security-sensitive code (gate 3): the boss must approve changes to it")
    return None


def is_protected(relative: str, patterns: tuple[str, ...]) -> bool:
    """A pattern like deploy/** also covers the directory itself, so removing or replacing it asks too."""
    candidates = (PurePosixPath(relative), PurePosixPath(relative) / "x")
    return any(c.full_match(pattern) for c in candidates for pattern in patterns)


# --- git -------------------------------------------------------------------------------------------

def check_git(args: list[str], ctx: Context, state: dict) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"} else 1
    if index >= len(args) or args[index] != "push":
        return None

    for arg in args[index + 1:]:
        if (
            arg in {"-f", "--force", "--mirror", "--delete", "-d", "--prune"}
            or arg.startswith("--force")
            or re.fullmatch(r"-[A-Za-z]*[fd][A-Za-z]*", arg)
            or re.search(r"(^|:)\+", arg)
            or arg.startswith(":")
        ):
            return "force, delete and mirror pushes are not allowed; ask the boss"
    reason = push_scan(ctx)
    if reason:
        return reason
    state["asks"].append("git push shares work outside this machine (gate): the boss must approve this push")
    return None


def push_scan(ctx: Context) -> str | None:
    """Every push is preceded by a whole-repo secret scan, so a leak cannot ride along. Fails closed."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import secret_scan

        found, complete = secret_scan.scan_repo(ctx.repo)
    except Exception as error:  # Cannot list or read the repo: never read as clean.
        return f"the pre-push secret scan could not run ({type(error).__name__}); fix that before pushing"
    if found:
        shown = ", ".join(f"{p.name}:{n} {k}" for p, n, k in found[:5])
        return f"the pre-push secret scan found {len(found)} finding(s) ({shown}); remove them or, for deliberate fakes, add a narrow entry to the allow list (the boss does that)"
    if not complete:
        return "the pre-push secret scan could not read some files, so it is incomplete"
    return None


# --- the project profile ---------------------------------------------------------------------------

def read_profile(repo: str) -> dict[str, str]:
    """The `key: value` lines inside the ```devteam-profile fenced block of .claude/project-profile.md."""
    try:
        text = (Path(repo) / PROFILE).read_text()
    except OSError:
        return {}
    block = re.search(r"```devteam-profile\n(.*?)```", text, re.S)
    lines = (block.group(1) if block else "").splitlines()
    pairs = (line.split(":", 1) for line in lines if ":" in line and not line.lstrip().startswith("#"))
    return {key.strip(): value.strip() for key, value in pairs}


def split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_protected(repo: str, profile: dict[str, str]) -> tuple[str, ...]:
    path = Path(repo) / profile.get("protected_paths_file", ".claude/protected-paths.txt")
    if not path.resolve().is_relative_to(Path(repo).resolve()):
        return ()  # A profile cannot point this at a file outside the project.
    try:
        lines = (line.strip() for line in path.read_text().splitlines())
    except OSError:
        return ()
    return tuple(line for line in lines if line and not line.startswith("#"))


# --- entry point -----------------------------------------------------------------------------------

def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if payload.get("tool_name") != "Bash":
            return 0
        command = payload["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("command is not text")
    except (ValueError, KeyError, TypeError, AttributeError):
        print("guard_bash: could not read the hook input; blocking to be safe", file=sys.stderr)
        return 2

    repo = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    profile = read_profile(repo)
    ctx = Context(
        repo=repo,
        cwd=payload.get("cwd") or os.getcwd(),
        agent_type=payload.get("agent_type"),
        scratchpad=payload.get("scratchpad_dir"),
        protected=load_protected(repo, profile),
        publish_dir=profile.get("build_output_dir") or None,
        secret_paths=split_list(profile.get("secret_locations", "")),
    )

    verdict = evaluate_full(command, ctx)
    if verdict.deny:
        print(f"Blocked by the devteam guard hook: {verdict.deny}.", file=sys.stderr)
        return 2
    if verdict.ask:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": verdict.ask,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
