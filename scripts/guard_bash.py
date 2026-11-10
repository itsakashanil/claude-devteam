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
inside double quotes), tracks cd and pushd, and blocks anything it cannot parse.

It fails closed: before any parsing, cannot_classify refuses the whole command ("the guard cannot classify this
command; write it plainly") when it holds a quoted or escaped ; & | ( ) < > (except a backslash-semicolon ending a find -exec), a number
or & glued before a redirection anywhere but the start of a word (`true&&2>f`), $' or $", a backslash-newline, or a #
that starts a word. Heredoc bodies are text and are skipped, except a body that feeds a shell (`bash <<EOF`), which is
checked as a script. The same check runs on every substitution, bash -c script and heredoc script. Plain quoted words
are fine; a quoted separator is not, so `echo "a|b"` and `git commit -m "fix (x)"` are refused: write it plainly.

Write targets (rule 3) come from the arguments of the writer commands and from redirections (> >> >| &> &>> <> >&file,
a number glued to the operator is a file descriptor). When cp, mv, install,
ln or rsync copy into a directory, directory/source-name is checked too. A target the guard cannot work out (a variable,
backtick, brace list, bracket class, ~+ or ~-, a relative path after a cd it cannot follow, more than 500 wildcard
matches) is blocked: write the literal path. * and ? are expanded on disk, wherever the path starts, and every match is
checked after following symlinks (not for rm, mv and ln, which act on the name itself). Shell writes into .git/hooks
and .git/config are blocked like .claude/. The permission prompt lists each different reason once, gate reasons (git
push) first, at most five, with the paths quoted and stripped of control characters.

With no profile the strictest generic rules still apply: deletes only inside the repo, no force pushes.

This is a guardrail against mistakes and prompt injection, not a sandbox: a program that builds its own command
line (python -c, a script file) is beyond what a text check can see. The real boundary is OS account separation.
"""

from __future__ import annotations

import glob
import itertools
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEPLOYER = "deployer"
MAX_DEPTH = 6
MAX_GLOB_MATCHES = 500
MAX_ASKS_SHOWN = 5
MAX_SHOWN_LENGTH = 120
PROFILE = ".claude/project-profile.md"
PUSH_ASK = "git push shares work outside this machine (gate): the boss must approve this push"
GATE_ASKS = {PUSH_ASK}  # Asks about sharing something: always listed first.

PUNCTUATION = "();<>|&\n"
SEPARATOR_CHARS = set(";&|()\n")  # A token made only of these ends a command; < and > are redirections.
SHELL_KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PRIVILEGE_TOOLS = {"su", "doas", "pkexec", "run0", "sudoedit", "chroot", "nsenter", "unshare"}
DELETERS = {"rm", "rmdir", "unlink", "shred"}
WRITERS = DELETERS | {"cp", "mv", "install", "ln", "rsync", "tee", "truncate", "dd", "touch", "chmod", "chown", "mkdir", "patch"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
GLOB_CHARS = set("*?[")
# Redirections: the operators that write a file, and the ones that only read (their operand is dropped, not checked).
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>", ">&"}
READ_REDIRECTS = {"<", "<<", "<<-", "<<<", "<&"}
REDIRECTS = WRITE_REDIRECTS | READ_REDIRECTS
PUNCTUATION_PIECES = re.compile(r"<\(|>\(|&>>|&>|<>|>>|>\||>&|<<<|<<-?|<&|[;&|()\n<>]")
# Commands that replace or remove the name itself: a symlink target is not followed. Everything else writes through a link.
DESTINATION_WRITERS = {"cp", "mv", "install", "ln", "rsync"}
NO_FOLLOW ={"rm", "rmdir", "unlink", "mv", "ln"}
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


class Unresolvable(Exception):
    """A write target the guard cannot work out (the shell would decide), so the command is blocked."""


# --- reading the command ---------------------------------------------------------------------------

class Operator(str):
    """A redirection operator that stood outside quotes in the command. A quoted or escaped `<` is a plain word."""


class FileDescriptor(str):
    """A number written directly against a redirection operator (`2>`, `5>f`), as opposed to a separate word."""


# Quoted or escaped < and > need no mark: cannot_classify refuses them first. {"<": "", ">": ""}
DIGIT_MARKS = {str(d): chr(0xE010 + d) for d in range(10)}
FD_MARKS = set(DIGIT_MARKS.values())
MARKS_BACK = {mark: char for char, mark in DIGIT_MARKS.items()}
DIGIT_START = " \t\r\n;|()"  # What may stand before a number for it to be a file descriptor (it must start a word).


def mark_quoting(command: str) -> str:
    """Replaces the digits of a file descriptor glued to a redirection operator (`2>`, `5>f`) with private characters,
    so it can be told from a word that merely ends in digits. cannot_classify has already refused every quoted or
    escaped operator, so quote state is only followed here to leave digits inside quotes alone (shlex's posix rules:
    single quotes are literal, a double quote ends only at an unescaped `"`, a backslash takes the next character)."""
    if any("" <= char <= "" for char in command):
        raise Unparseable("private-use characters in the command")
    out: list[str] = []
    index, quote, length = 0, "", len(command)
    while index < length:
        char = command[index]
        if quote == "'":
            out.append(char)
            quote = "" if char == "'" else quote
        elif quote == '"':
            if char == "\\" and index + 1 < length:  # An escaped character never closes the quote.
                out.append(char)
                index += 1
                out.append(command[index])
            else:
                out.append(char)
                quote = "" if char == '"' else quote
        elif char == "\\" and index + 1 < length:
            out.append(char)
            index += 1
            out.append(command[index])
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char.isdigit() and (index == 0 or command[index - 1] in DIGIT_START):
            end = index
            while end < length and command[end].isdigit():
                end += 1
            glued = end < length and command[end] in "<>"
            out.append("".join(DIGIT_MARKS.get(d, d) for d in command[index:end]) if glued else command[index:end])
            index = end
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def restore_marks(text: str) -> str:
    return "".join(MARKS_BACK.get(char, char) for char in text)


CANNOT_CLASSIFY = "the guard cannot classify this command; write it plainly"
QUOTE_UNSAFE = set(";&|()<>")  # Inside quotes or after a backslash these are where shlex and bash would part ways.
FIND_EXEC_WORDS = {"-exec", "-execdir", "-ok", "-okdir"}
HEREDOC_AT = re.compile(r"<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
LAST_WORD_END = re.compile(r"[ \t]*(?:\Z|\n|[;&|)])")


def cannot_classify(text: str) -> str | None:
    """A check on the raw text, before any parsing: the reason to block, or None. It follows only quote and escape
    state, and refuses what the later steps cannot read the way bash does:

      1. a quoted or escaped ; & | ( ) < > (only `\\;` as the last word of a find -exec/-execdir/-ok/-okdir is
         allowed: the shell passes it to find as the end of the command);
      2. a number or & glued before a redirection operator anywhere but at the start of a word (`true&&2>f`);
      3. $' and $" (ANSI-C and locale quoting) outside single quotes;
      4. a backslash followed by a newline outside single quotes (bash joins the lines, shlex does not);
      5. a # that starts a word outside quotes (a comment to bash, an ordinary word to shlex).

    The body of a heredoc (after a real, unquoted <<WORD, up to its closing line) is text and is skipped; a heredoc that
    feeds a shell is checked on its own as a script (see split_heredocs). A stricter result than bash is fine, a looser
    one is not: when unsure this says no."""
    index, length = 0, len(text)
    quote = ""
    command_start = 0  # Where the current simple command began (only used to recognise a find -exec).
    word_start = True
    pending: list[tuple[bool, str]] = []  # Heredocs opened on the current line: (tabs stripped, delimiter).
    while index < length:
        char = text[index]
        after = text[index + 1] if index + 1 < length else ""
        if quote:
            if char == quote:
                quote = ""
            elif char in QUOTE_UNSAFE or (char == "$" and after == "'" and quote == '"'):
                return CANNOT_CLASSIFY
            elif char == "\\" and quote == '"':
                if after == "\n" or after in QUOTE_UNSAFE:
                    return CANNOT_CLASSIFY
                index += 1  # The escaped character never closes the quote.
            index += 1
            continue

        if char == "\\":
            if after == "\n" or (after in QUOTE_UNSAFE and not ends_find_exec(text, command_start, index, word_start)):
                return CANNOT_CLASSIFY
            word_start = False
            index += 2
        elif char in "'\"":
            quote, word_start = char, False
            index += 1
        elif char == "$" and after in {"'", '"'}:
            return CANNOT_CLASSIFY
        elif char == "#" and word_start:
            return CANNOT_CLASSIFY
        elif char in "<>":
            if glued_before_redirect(text, index):
                return CANNOT_CLASSIFY
            match = HEREDOC_AT.match(text, index) if char == "<" and text[index - 1 : index] != "<" else None
            if match:
                pending.append((match.group(0).startswith("<<-"), match.group(2)))
                word_start = False
                index = match.end()
            else:
                word_start = True
                index += 1
        elif char == "\n":
            resume = heredoc_bodies_end(text, index, pending)
            pending = []
            command_start, word_start = resume + 1, True
            index = resume + 1
        elif char in ";|()" or (char == "&" and after != ">" and text[index - 1 : index] not in {"<", ">"}):
            command_start, word_start = index + 1, True  # A separator; the & of &>, >& and <& is part of a redirection.
            index += 1
        elif char in " \t&":
            word_start = True
            index += 1
        else:
            word_start = False
            index += 1
    return None


def glued_before_redirect(text: str, index: int) -> bool:
    """True when the < or > at index has an & or a number in front of it that is not the plain form: `&>`, `2>` and
    `5>f` are fine at the start of a word (after a space, a newline or the start of the text), `&&2>`, `;2>`, `|2>`,
    `&2>` and `&&>` are not. A number that ends a longer word (`x5>f`) is part of that word."""
    if text[index - 1 : index] == "&":
        return index > 1 and text[index - 2] not in " \t\n"
    start = index
    while start > 0 and text[start - 1].isdigit():
        start -= 1
    return 0 < start < index and text[start - 1] in ";&|()"


def ends_find_exec(text: str, command_start: int, index: int, word_start: bool) -> bool:
    """True for a `\\;` at index that is the last word of a find command with -exec, -execdir, -ok or -okdir."""
    if not word_start or text[index + 1 : index + 2] != ";":
        return False
    words = text[command_start:index].split()
    return (
        bool(words)
        and os.path.basename(words[0]) == "find"
        and bool(FIND_EXEC_WORDS & set(words))
        and LAST_WORD_END.match(text, index + 2) is not None
    )


def heredoc_bodies_end(text: str, newline: int, pending: list[tuple[bool, str]]) -> int:
    """Where the heredoc bodies that start after this newline end: the index of the newline (or the end of the text)
    after the last closing line. With no body that closes (so no real heredoc) it is the newline itself."""
    resume, start = newline, newline + 1
    for dash, tag in pending:
        position = start
        while position <= len(text):
            line_end = text.find("\n", position)
            end = len(text) if line_end == -1 else line_end
            line = text[position:end]
            if (line.lstrip("\t") if dash else line) == tag:
                resume, start = end, end + 1
                break
            if line_end == -1:
                break  # No closing line: this was not a heredoc.
            position = line_end + 1
    return resume


def tokenize(command: str) -> list[list[str]]:
    """Splits into simple commands at ; & | ( ) and newlines that are outside quotes. Quoted text is left exactly as
    written, so a script passed to `bash -c "..."` keeps its own newlines for the recursive check. Redirection
    operators outside quotes come out as Operator, numbers glued to one as FileDescriptor, everything else as str."""
    lexer = shlex.shlex(mark_quoting(command), posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"  # Not the newline: that is a separator.
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise Unparseable(str(error)) from error

    commands: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= set(PUNCTUATION):
            for piece in PUNCTUATION_PIECES.findall(token):
                if set(piece) <= SEPARATOR_CHARS:
                    commands.append([])
                elif piece in REDIRECTS:
                    commands[-1].append(Operator(piece))  # Taken out again by split_redirections.
                else:
                    commands[-1].append(piece)
        elif token and all(char in FD_MARKS for char in token):
            commands[-1].append(FileDescriptor(restore_marks(token)))
        else:
            commands[-1].append(restore_marks(token))
    return [c for c in commands if c]


def split_redirections(tokens: list[str]) -> tuple[list[str], list[str], str | None]:
    """Takes redirections out of one simple command. Returns (the words left, every file written, a problem).

    A write operator with no target is a problem (fail closed). Only a number glued to an operator in the raw text
    (`2>`, `5>f`) is a file descriptor and is dropped; a separate word `5` stays a word. Quoted or escaped `<` and `>`
    are words too. Read operators and here-docs are removed with their operand, which is a file read, never an
    argument. `>&word` writes a file unless the word is a number or `-`. T-003 can reuse this to find every write
    target of a command."""
    kept: list[str] = []
    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, FileDescriptor):
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if not isinstance(following, Operator):
                kept.append(str(token))  # Not in front of an operator after all: an ordinary word.
            index += 1
            continue
        if not isinstance(token, Operator):
            kept.append(token)
            index += 1
            continue
        operand = tokens[index + 1] if index + 1 < len(tokens) else None
        if isinstance(operand, Operator):
            operand = None
        index += 2 if operand is not None else 1
        if token in WRITE_REDIRECTS:
            if operand is None:
                return kept, targets, "a redirection has no target the guard can read; write the file name right after it"
            if not (token == ">&" and re.fullmatch(r"\d+-?|-", operand)):
                targets.append(operand)
    return kept, targets, None


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
            body = "\n".join(lines[index:end])
            if feeds_shell(line[: match.start()]):
                scripts.append(body)  # The shell runs the body: it is a script like bash -c's.
            elif not quote:
                scripts.extend(substitutions(body))
            index = end + 1
    return "\n".join(kept), scripts


def feeds_shell(before: str) -> bool:
    """True when the command in front of a heredoc operator is a shell (`bash <<EOF`, `env sh <<EOF`)."""
    words = [word for word in re.split(r"[;&|()]", before)[-1].split() if not re.search(r"[<>]", word)]
    unwrapped = unwrap(words)
    return bool(unwrapped) and os.path.basename(unwrapped[0]) in SHELLS


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

    unclassifiable = cannot_classify(command)  # On the raw text: heredoc bodies are cut out only after this.
    if unclassifiable:
        return Verdict(deny=unclassifiable)

    state = {"cwd": ctx.cwd, "asks": [], "depth": 0}
    try:
        body, hidden = split_heredocs(command)
        for script in [body, *hidden]:
            reason = check_script(script, ctx, state)
            if reason:
                return Verdict(deny=reason)
    except Unparseable:
        return Verdict(deny="the command could not be parsed safely (unbalanced quotes or brackets?); rewrite it more simply")

    return Verdict(ask=join_asks(state["asks"]))


def join_asks(asks: list[str]) -> str | None:
    """Every different reason once, at most MAX_ASKS_SHOWN of them, so the boss sees what he approves. Gate reasons
    (a push or share) come first, so they are never in the "and N more" tail; the rest keep the order they came in."""
    unique = sorted(dict.fromkeys(asks), key=lambda reason: reason not in GATE_ASKS)
    if not unique:
        return None
    shown = "; ".join(unique[:MAX_ASKS_SHOWN])
    hidden = len(unique) - MAX_ASKS_SHOWN
    return f"{shown}; and {hidden} more" if hidden > 0 else shown


def quoted(text: str) -> str:
    """Text taken from the command, made safe to show in a prompt: control characters removed, cut short, quoted."""
    clean = "".join(char for char in text if char.isprintable())
    if len(clean) > MAX_SHOWN_LENGTH:
        clean = clean[:MAX_SHOWN_LENGTH] + "..."
    return "'" + clean.replace("\\", "\\\\").replace("'", "\\'") + "'"


def evaluate(command: str, ctx: Context) -> str | None:
    """The reason to block the command, or None to let it through."""
    return evaluate_full(command, ctx).deny


def evaluate_ask(command: str, ctx: Context) -> str | None:
    """The reason the boss must be asked first (only meaningful when evaluate() returns None)."""
    return evaluate_full(command, ctx).ask


def check_script(script: str, ctx: Context, state: dict) -> str | None:
    if state["depth"] > MAX_DEPTH:
        return "commands nested too deeply to check"

    unclassifiable = cannot_classify(script)  # Substitutions, heredoc scripts and bash -c scripts come through here.
    if unclassifiable:
        return unclassifiable

    inner_state = {"cwd": state["cwd"], "asks": state["asks"], "depth": state["depth"] + 1}
    for inner in substitutions(script):
        reason = check_script(inner, ctx, dict(inner_state))
        if reason:
            return reason

    return check_commands(tokenize(script), ctx, state)


def check_commands(commands: list[list[str]], ctx: Context, state: dict) -> str | None:
    for tokens in commands:
        words, targets, problem = split_redirections(tokens)
        if problem:
            return problem
        for target in targets:
            reason = control_check(target, ctx, state)  # A redirection writes through a symlink.
            if reason:
                return reason
        reason = check_one(unwrap(words), ctx, state)
        if reason:
            return reason
    return None


def check_one(tokens: list[str], ctx: Context, state: dict) -> str | None:
    if not tokens:
        return None

    program, args = os.path.basename(tokens[0]), tokens[1:]

    if program == "sudo" or program in PRIVILEGE_TOOLS:
        return check_sudo(tokens, ctx)

    if program == "cd":
        state["cwd"] = next_directory(args, state["cwd"])
        return None

    if program == "pushd":
        # Without a directory it swaps the top two; with an option (-n) or +N/-N it may not move at all: unknown.
        plain = bool(args) and not any(a.startswith(("-", "+")) for a in args)
        state["cwd"] = next_directory(args, state["cwd"]) if plain else None
        return None

    if program == "popd":
        state["cwd"] = None  # The directory stack is not tracked, so where we are is unknown from here on.
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

    unclassifiable = cannot_classify(script)  # Before the heredoc bodies are cut out of it.
    if unclassifiable:
        return unclassifiable

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
        if path.startswith("~"):
            return False  # ~+, ~- or an unknown user: left as written, it would be read against the hook's own directory.
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
    if target == "-" or target.startswith("+") or "$" in target or "`" in target or "{" in target or GLOB_CHARS & set(target):
        return None
    if target.startswith("~"):
        expanded = os.path.expanduser(target)
        return None if expanded.startswith("~") else os.path.normpath(expanded)
    if target.startswith("/"):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(cwd, target)) if cwd else None


# --- writing to the team's own rules and to protected code ------------------------------------------

def check_writes(program: str, args: list[str], ctx: Context, state: dict) -> str | None:
    follow = program not in NO_FOLLOW
    for target in write_targets(program, args):
        reason = control_check(target, ctx, state, follow=follow)
        if reason:
            return reason
    for target in directory_destinations(program, args, state["cwd"]):
        reason = control_check(target, ctx, state, follow=follow)
        if reason:
            return reason
    return None


def directory_destinations(program: str, args: list[str], cwd: str | None) -> list[str]:
    """When cp, mv, install, ln or rsync copy into a directory, the real write targets are directory/name for each
    source: `cp -r /x/hooks .git/` writes .git/hooks. The directory is the one given with -t, or the last word when it
    ends in /, is . or .., or exists as a directory (a link to one counts). A source that is the find marker {} has an
    unknown name, so the names that matter (.claude, hooks, config) are checked instead."""
    if program not in DESTINATION_WRITERS:
        return []

    sources: list[str] = []
    given: str | None = None
    options_done, take_next = False, False
    for arg in args:
        attached = re.fullmatch(r"-[A-Za-z]*t(.+)", arg)
        if take_next:
            given, take_next = arg, False
        elif not options_done and arg == "--":
            options_done = True
        elif not options_done and arg.startswith("--target-directory="):
            given = arg.split("=", 1)[1]
        elif not options_done and (arg == "--target-directory" or re.fullmatch(r"-[A-Za-z]*t", arg)):
            take_next = True
        elif not options_done and attached:
            given = attached.group(1)
        elif not options_done and arg.startswith("-"):
            continue
        else:
            sources.append(arg)

    destination = given
    if destination is None:
        if len(sources) < 2:
            return []
        destination = sources.pop()
        if not is_directory(destination, cwd):
            return []

    base = destination.rstrip("/")
    found: list[str] = []
    for source in sources:
        name = os.path.basename(source.rstrip("/"))
        if source == "{}":
            found += [f"{base}/{unknown}" for unknown in (".claude", "hooks", "config")]
        elif name and name not in {".", ".."}:
            found.append(f"{base}/{name}")
    return found


def is_directory(path: str, cwd: str | None) -> bool:
    """True for a path that ends in /, is . or .., or exists as a directory (a link to one counts). A path the guard
    cannot resolve is not (it is already refused as a write target of its own)."""
    if path.endswith("/") or os.path.basename(path) in {".", ".."}:
        return True
    try:
        absolute = absolute_target(path, cwd)
    except Unresolvable:
        return False
    return not (GLOB_CHARS & set(absolute)) and os.path.isdir(absolute)


def write_targets(program: str, args: list[str]) -> list[str]:
    """Every word of the command that names a file it may write: plain arguments, `--opt=value` and `-tDIR` values,
    dd's of=, and for sed -i only the files (its script and -e/-f values are not paths)."""
    if program == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if program == "sed":
        return sed_files(args)

    targets: list[str] = []
    options_done = False
    for arg in args:
        attached = re.fullmatch(r"-[A-Za-z]*t(.+)", arg)
        if not options_done and arg == "--":
            options_done = True
        elif not options_done and arg.startswith("--") and "=" in arg:
            targets.append(arg.split("=", 1)[1])
        elif not options_done and attached:
            targets.append(attached.group(1))
        elif not options_done and arg.startswith("-"):
            continue
        else:
            targets.append(arg)
    return targets


def sed_files(args: list[str]) -> list[str]:
    files: list[str] = []
    script_seen, options_done, skip_next = False, False, False
    for arg in args:
        if skip_next:
            skip_next = False
        elif options_done or not arg.startswith("-"):
            if script_seen:
                files.append(arg)
            else:
                script_seen = True  # The first plain word is the script when no -e or -f was given.
        elif arg == "--":
            options_done = True
        elif arg in {"-e", "-f", "--expression", "--file"} or re.fullmatch(r"-[A-HJ-Za-hj-z]*[ef]", arg):
            script_seen, skip_next = True, True
        elif arg.startswith(("-e", "-f", "--expression=", "--file=")):
            script_seen = True
    return files


def absolute_target(path: str, cwd: str | None) -> str:
    """The absolute text of a write target, or Unresolvable when the shell (not us) would decide what it means:
    a variable, backtick, brace list, bracket class, ~+ or ~-, or a relative path after a cd we cannot follow."""
    if "$" in path or "`" in path or "[" in path or ("{" in path and path != "{}"):  # {} is find -exec's own marker.
        raise Unresolvable()
    if path.startswith("~"):
        expanded = os.path.expanduser(path)
        if expanded.startswith("~"):
            raise Unresolvable()
        return expanded
    if path.startswith("/"):
        return path
    if cwd is None:
        raise Unresolvable()
    return os.path.join(cwd, path)


def repo_relatives(path: str, ctx: Context, cwd: str | None, follow: bool = True) -> list[str]:
    """Every place inside the repo (relative to its root, "" for the root) this write target can reach.

    `*` and `?` are expanded on disk (hidden names included, at most MAX_GLOB_MATCHES matches), and the part before
    the first wildcard is checked too. With follow, a symlink as the last name counts as what it points to."""
    absolute = absolute_target(path, cwd)
    repo = os.path.realpath(ctx.repo)
    parts = absolute.split("/")
    cut = next((i for i, part in enumerate(parts) if GLOB_CHARS & set(part)), None)

    candidates = [absolute]
    if cut is not None:
        # Expand up to the last wildcard name (a link found there may lead into the repo, wherever the path starts),
        # then put the plain rest of the path back on each match, so a new file below a matched link is checked too.
        last = max(i for i, part in enumerate(parts) if GLOB_CHARS & set(part))
        tail = "/".join(parts[last + 1:])
        found = list(itertools.islice(glob.iglob("/".join(parts[: last + 1]), include_hidden=True), MAX_GLOB_MATCHES + 1))
        if len(found) > MAX_GLOB_MATCHES:
            raise Unresolvable()
        candidates = ["/".join(parts[:cut]) or "/"] + [f"{match}/{tail}" if tail else match for match in found]

    places: list[str] = []
    for candidate in candidates:
        for resolved in dict.fromkeys([resolve(candidate), os.path.realpath(candidate)] if follow else [resolve(candidate)]):
            if resolved == repo:
                places.append("")
            elif resolved.startswith(repo + "/"):
                places.append(resolved[len(repo) + 1:])
    return places


def is_git_internal(relative: str) -> bool:
    """.git/hooks and .git/config: a planted hook or alias can push where this guard never sees it."""
    return relative in {".git/config", ".git/hooks"} or relative.startswith(".git/hooks/")


def control_check(path: str, ctx: Context, state: dict, searching: bool = False, follow: bool = True) -> str | None:
    """Deny anything under .claude/ and shell writes into .git/hooks and .git/config; ask the boss about protected
    paths; deny a target that cannot be worked out (fail closed)."""
    try:
        relatives = repo_relatives(path, ctx, state["cwd"], follow=follow)
    except Unresolvable:
        return (
            f"cannot tell where the write target {quoted(path)} points (a variable, backtick, brace list, bracket class, "
            "~+ or ~-, a path after a cd the guard cannot follow, or more than 500 wildcard matches); write the literal path"
        )

    for relative in relatives:
        if relative == ".claude" or relative.startswith(".claude/"):
            return "the team's own rules under .claude/ cannot be changed from the shell; only the boss edits them"
        if is_git_internal(relative):
            return "shell writes into .git/hooks and .git/config are blocked: a planted hook could push where the guard cannot see"

    if not searching:
        for relative in relatives:
            if is_protected(relative, ctx.protected):
                state["asks"].append(f"{quoted(relative)} is security-sensitive code (gate 3): the boss must approve changes to it")
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
    state["asks"].append(PUSH_ASK)
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
