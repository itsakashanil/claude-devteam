# Proposal: <name>

<!-- Builder: fill every <...> except C (C only exists after review). Delete this comment. -->
What: <what changes for the team, and why the agents cannot install it themselves>
Task: <task-id>, branch task/<task-id>-<slug>. Plan, review and reviewed commit: .team-work/<task-id>/.

## Read this file from the reviewed commit, not from your working tree
An edit after review could weaken step 0 or add an install line, so never follow the working-tree copy.
1. Take the full 40-character reviewed commit from the manager's hand-over message.
2. Check it equals .team-work/<task-id>/reviewed-commit and the REVIEWED line of the review, and that
   `git --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false log --oneline task/<task-id>-<slug>` contains it.
3. Open this file with `git -c core.hooksPath=/dev/null -c core.fsmonitor=false show <C>:docs/proposed/<name>/README.md | less` and run every step from that output.

## Before you start
1. Stop every agent session in this repository and keep them stopped until step 4 passes.
2. Use one plain bash terminal (not inside Claude Code) in the repository root, for all steps: they share variables.
3. A failed check prints `STOP: <reason>` and installs nothing. Do not continue after a STOP.

## Step 0: the source is exactly the reviewed commit
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
C=<full reviewed commit from the hand-over message>
BASE=<builder: the main commit the targets were read from>
B=task/<task-id>-<slug>
P=docs/proposed/<name>
# one line per file, no blank lines, no spaces in paths: mode source(relative to $P) target(relative to the repo root)
# modes: 0755 for scripts and hooks, 0644 otherwise
MAP='<0644 agents/example.md .claude/agents/example.md>'
check_source() (
  fail() { echo "STOP: $*"; exit 1; }
  bad=$(g config --local --name-only --get-regexp '^(core\.(fsmonitor|hookspath|sshcommand|pager|editor)|alias\..*|filter\..*|diff\..*\.textconv|include\.path|includeif\..*)$' | head -n 1)
  [ -z "$bad" ] || fail "the local git config sets $bad, which can run programs; remove it first"
  [ "${#C}" -eq 40 ] || fail "C must be the full 40-character hash"
  [ "$(g rev-parse --verify --quiet "$C^{commit}")" = "$C" ] || fail "C is not a commit here"
  [ "$(g branch --show-current)" = "$B" ] || fail "not on branch $B"
  g merge-base --is-ancestor "$C" HEAD || fail "this branch does not contain the reviewed commit"
  [ -z "$(g status --porcelain --ignored --untracked-files=all -- "$P")" ] || fail "$P has changed, untracked or git-ignored files"
  g diff --quiet "$C" -- "$P" || fail "$P differs from the reviewed commit"
  while read -r mode src dst extra; do
    [ -n "$dst" ] && [ -z "$extra" ] || fail "bad MAP line: $mode $src $dst $extra"
    case "$mode" in 0644|0755) ;; *) fail "mode $mode for $dst" ;; esac
    case "/$dst/" in //*|*/../*|/.git/*) fail "target $dst is outside the repository or in .git" ;; esac
    [ "$(g cat-file -t "$C:$P/$src" 2>/dev/null)" = blob ] || fail "$P/$src is not a file tracked at the reviewed commit"
  done <<< "$MAP"
  echo "OK: step 0, source is $C"
)
check_source
```

## Step 1: the targets are what the proposal was built on
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
check_targets() (
  fail() { echo "STOP: $*"; exit 1; }
  g cat-file -e "$BASE^{commit}" || fail "BASE is not a commit"
  while read -r mode src dst; do
    [ ! -L "$dst" ] || fail "$dst is a symlink"
    if g cat-file -e "$BASE:$dst" 2>/dev/null; then
      g diff --quiet "$BASE" HEAD -- "$dst" && [ -z "$(g status --porcelain -- "$dst")" ] \
        || fail "$dst changed since BASE; ask for the proposal to be rebuilt"
    else
      [ ! -e "$dst" ] || fail "$dst should be new but already exists"
    fi
  done <<< "$MAP"
  echo "OK: step 1, targets unchanged since BASE"
)
check_targets
```

## Step 2: read the change (the only step that relies on your reading)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
T=$(mktemp -d)
while read -r mode src dst; do
  mkdir -p "$T/$(dirname "$dst")" && g cat-file blob "$C:$P/$src" > "$T/$dst"
  echo "== $dst ($mode)"; if [ -e "$dst" ]; then diff -u "$dst" "$T/$dst"; else diff -u /dev/null "$T/$dst"; fi
done <<< "$MAP" | less
rm -rf "$T"
```

## Step 3: install from the reviewed commit (checks again first)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
if check_source && check_targets; then
  ( while read -r mode src dst; do
      tmp=$(mktemp) && g cat-file blob "$C:$P/$src" > "$tmp" \
        && install -D -m "$mode" "$tmp" "$dst" && rm -f "$tmp" || exit 1
      echo "installed $dst ($mode)"
    done <<< "$MAP" ) && echo "OK: step 3, go to step 4" || echo "STOP: install stopped part-way; run step 5"
fi
```

## Step 4: tests, with test-only overrides unset
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
env -u <EACH_TEST_ONLY_VARIABLE, or delete the -u> <test command> || echo "STOP: tests failed; run step 5"
```
Expected: <N> tests, all passing. A different count is a STOP.

## Step 5: rollback (replaced files back to BASE, new files removed)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
while read -r mode src dst; do
  if g cat-file -e "$BASE:$dst" 2>/dev/null; then g restore --source="$BASE" --staged --worktree -- "$dst"
  else rm -f -- "$dst"; fi
done <<< "$MAP"
g status --short
```
Empty folders created by step 3 stay; remove them by hand if you want.

## Step 6: finish
1. Restart Claude Code sessions: agents, settings and hooks are read at session start.
2. Commit on the task branch, the installed files and the removal of the proposal together:
   `g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }` first, then `while read -r mode src dst; do g add -- "$dst"; done <<< "$MAP"; g rm -r -q -- "$P"; g commit -m "Install <name> (reviewed $C)"`
3. You merge the task branch into main yourself; the manager never merges changes under .claude/ or CLAUDE.md.
   Then tell the manager "installed" so it can finish the report.

## What these checks cannot see
- The checks prove the files are the same bytes as the reviewed commit, not that the change is safe. That is the reviewer's job.
- The hash reaches you through the manager: reviewed-commit and the review are written by it. You can also run
  `git --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false log --oneline "$B"` and `git --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false show --stat <C>` to see what the commit is.
- Step 0 stops on program-running settings in the repository's own git config (fsmonitor, hooksPath, sshCommand, pager,
  editor, aliases, filters, textconv, includes). It does not read ~/.gitconfig or the system config, which can set the
  same things and are writable by the same user; check them yourself if you do not trust the machine.
- Agents run as your user. Beyond git config, they could plant a symlink in a parent directory of a target: the target
  itself is checked, its parents are not.
- Nothing proves you ran step 0. Targets outside the repository are out of scope, and paths with spaces are not supported.
