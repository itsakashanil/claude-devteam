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
   Every block starts with the helper `g`, which runs git with hooks and fsmonitor switched off. Run the blocks whole.
3. A failed check prints `STOP: <reason>`. Do not continue after a STOP. A STOP before step 3 has installed nothing; a STOP in
   step 3 (part-way) or step 4 means run step 5, which undoes only what step 3 installed.

## Step 0: the source is exactly the reviewed commit
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
C=<full reviewed commit from the hand-over message>
BASE=<builder: the main commit the targets were read from>
MAIN=main
B=task/<task-id>-<slug>
P=docs/proposed/<name>
# one line per file, no blank lines, no spaces in paths: mode source(relative to $P) target(relative to the repo root)
# modes: 0755 for scripts and hooks, 0644 otherwise; paths may use only A-Z a-z 0-9 . _ - /
MAP='<0644 agents/example.md .claude/agents/example.md>'
check_map() (
  fail() { echo "STOP: $*"; exit 1; }
  export LC_ALL=C
  safe_path() {
    case "$1" in ''|*[!A-Za-z0-9._/-]*|-*|/*|./*|*/./*|*/.|.|..|../*|*/../*|*/..|*//*|*/) return 1 ;; esac
    case "/$1/" in */.git/*) return 1 ;; esac
  }
  while read -r mode src dst extra; do
    [ -n "$dst" ] && [ -z "$extra" ] || fail "bad MAP line: $mode $src $dst $extra"
    case "$mode" in 0644|0755) ;; *) fail "mode $mode for $dst" ;; esac
    safe_path "$src" || fail "unsafe source path $src"
    safe_path "$dst" || fail "unsafe target $dst (outside the repository, inside .git, or odd characters)"
    [ "$(g cat-file -t "$C:$P/$src" 2>/dev/null)" = blob ] || fail "$P/$src is not a file tracked at the reviewed commit"
  done <<< "$MAP"
)
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
  rest=(. ':(exclude)docs/reports' ':(exclude)docs/BACKLOG.md')
  g diff --quiet "$C" HEAD -- "${rest[@]}" || fail "HEAD differs from the reviewed commit outside docs/reports and docs/BACKLOG.md; ask for a new review"
  [ -z "$(g status --porcelain --untracked-files=all -- "${rest[@]}")" ] || fail "the working tree has changes outside the reviewed commit (docs/reports and docs/BACKLOG.md excepted); look with: g status"
  check_map || exit 1
  echo "OK: step 0, source is $C"
)
verify_installed() (
  fail() { echo "STOP: $*"; exit 1; }
  set -o pipefail
  check_map || exit 1
  while read -r mode src dst; do
    [ ! -L "$dst" ] || fail "$dst is a symlink"
    [ -f "$dst" ] || fail "$dst is missing"
    g cat-file blob "$C:$P/$src" | cmp -s -- - "$dst" || fail "$dst is not the reviewed content"
    [ "$(stat -c %a -- "$dst")" = "${mode#0}" ] || fail "$dst has mode $(stat -c %a -- "$dst"), expected ${mode#0}"
  done <<< "$MAP"
  echo "OK: installed files match the reviewed commit"
)
check_source
```

## Step 1: the targets are what the proposal was built on
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
check_targets() (
  fail() { echo "STOP: $*"; exit 1; }
  g cat-file -e "$BASE^{commit}" || fail "BASE is not a commit"
  g cat-file -e "$MAIN^{commit}" || fail "MAIN is not a branch here"
  while read -r mode src dst; do
    [ ! -L "$dst" ] || fail "$dst is a symlink"
    if g cat-file -e "$BASE:$dst" 2>/dev/null; then
      g diff --quiet "$BASE" HEAD -- "$dst" && [ -z "$(g status --porcelain -- "$dst")" ] \
        || fail "$dst changed since BASE; ask for the proposal to be rebuilt"
      g diff --quiet "$BASE" "$MAIN" -- "$dst" \
        || fail "$dst changed on $MAIN since BASE; ask for the proposal to be rebuilt"
    else
      [ ! -e "$dst" ] || fail "$dst should be new but already exists"
      ! g cat-file -e "$MAIN:$dst" 2>/dev/null || fail "$dst should be new but exists on $MAIN; ask for the proposal to be rebuilt"
    fi
  done <<< "$MAP"
  echo "OK: step 1, targets unchanged since BASE"
)
check_targets
```

## Step 2: read the change (the only step that relies on your reading)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
(
  T=$(mktemp -d) || exit 1
  trap 'rm -rf -- "$T"' EXIT
  trap 'exit 1' INT TERM
  while read -r mode src dst; do
    mkdir -p -- "$T/$(dirname -- "$dst")" && g cat-file blob "$C:$P/$src" > "$T/$dst" || exit 1
    echo "== $dst ($mode)"
    if [ -e "$dst" ]; then diff -u -- "$dst" "$T/$dst"; else diff -u -- /dev/null "$T/$dst"; fi
  done <<< "$MAP" | less
)
```

## Step 3: install from the reviewed commit (checks again first)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
if check_source && check_targets; then
  ( tmp=$(mktemp) && trap 'rm -f -- "$tmp"' EXIT \
    && while read -r mode src dst; do
         g cat-file blob "$C:$P/$src" > "$tmp" && install -D -m "$mode" -- "$tmp" "$dst" || exit 1
         echo "installed $dst ($mode)"
       done <<< "$MAP" ) && echo "OK: step 3, go to step 4" || echo "STOP: install stopped part-way; run step 5"
fi
```

## Step 4: tests, with test-only overrides unset, then check the installed files again
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
env -u <EACH_TEST_ONLY_VARIABLE, or delete the -u> <test command> || echo "STOP: tests failed; run step 5"
verify_installed
```
Expected: <N> tests, all passing. A different count is a STOP. A STOP from `verify_installed` means a file changed after step 3
(the tests, or something else, edited it): do not go on; run step 5, which leaves any file you edited yourself alone.

## Step 5: rollback (undoes only what step 3 installed)
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
rollback() (
  fail() { echo "STOP: $*"; exit 1; }
  set -o pipefail
  left=0
  while read -r mode src dst; do
    if [ -L "$dst" ]; then
      echo "$dst is a symlink; left alone"; left=$((left + 1))
    elif g cat-file blob "$C:$P/$src" | cmp -s -- - "$dst"; then
      # this is the file step 3 installed: put the original back, or remove a file that was new
      if g cat-file -e "$BASE:$dst" 2>/dev/null; then g restore --source="$BASE" --staged --worktree -- "$dst"
      else rm -f -- "$dst"; fi
      echo "rolled back $dst"
    elif [ ! -e "$dst" ] && ! g cat-file -e "$BASE:$dst" 2>/dev/null; then
      echo "$dst: nothing to undo"
    elif g cat-file blob "$BASE:$dst" 2>/dev/null | cmp -s -- - "$dst"; then
      echo "$dst: nothing to undo"
    else
      echo "$dst is neither the installed nor the original version; left alone"; left=$((left + 1))
    fi
  done <<< "$MAP"
  [ "$left" -eq 0 ] || fail "$left file(s) left alone; look at them yourself"
  echo "OK: step 5, rolled back"
)
check_map && rollback
g status --short
```
Empty folders created by step 3 stay; remove them by hand if you want.

## Step 6: finish
```bash
g() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
finish() (
  fail() { echo "STOP: $*"; exit 1; }
  verify_installed || exit 1
  [ -z "$(g diff --cached --name-only)" ] || fail "something is already staged; unstage it first"
  while read -r mode src dst; do g add -- "$dst" || fail "could not stage $dst"; done <<< "$MAP"
  g rm -r -q -- "$P" || fail "could not remove $P"
  g commit -q --no-gpg-sign -m "Install ${P##*/} (reviewed $C)" || fail "the commit failed"
  echo "OK: step 6, committed on $B; restart your Claude Code sessions now"
)
finish
```
1. The commit comes first and the sessions restart after it: agents, settings and hooks are read at session start.
2. You merge the task branch into main yourself; the manager never merges changes under .claude/ or CLAUDE.md. Type the git
   commands with the same switches: `git -c core.hooksPath=/dev/null -c core.fsmonitor=false checkout main`, then
   `git -c core.hooksPath=/dev/null -c core.fsmonitor=false merge --no-ff "$B"`. Then tell the manager "installed" so it can finish the report.

## What these checks cannot see
- The checks prove the files are the same bytes as the reviewed commit, not that the change is safe. That is the reviewer's job.
- The hash reaches you through the manager: reviewed-commit and the review are written by it. You can also run
  `git --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false log --oneline "$B"` and `git --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false show --stat <C>` to see what the commit is.
- Commits after the reviewed commit: step 0 stops if HEAD or the working tree differs from it anywhere except docs/reports and
  docs/BACKLOG.md (the manager writes those after review and they run no code). Git-ignored files outside the proposal folder are
  not checked, so a test run can still load them; the tests that step 4 runs are the working tree's, which step 0 has just compared with C.
- Git config: step 0 stops on program-running settings in the repository's own `.git/config` only, and only the common keys
  (fsmonitor, hooksPath, sshCommand, pager, editor, aliases, filters, textconv, includes). It does not read config.worktree,
  `diff.<driver>.command`, `diff.external`, `merge.<driver>.driver`, `pager.<command>`, `gpg.program`, GIT_* environment variables,
  `~/.gitconfig` or the system config. The helper `g` switches off hooks and fsmonitor, not those. Check them yourself if you do not trust the machine.
- Your own shell: a fake `git`, `install`, `cmp` or `bash` earlier on PATH, `~/.bashrc`, aliases and GIT_* variables are writable by the
  agents (they run as your user) and are not checked. The same goes for the installed plugin copy of this template, which the reviewer
  compares the README against: a changed plugin copy would make a changed README look right.
- Agents could also plant a symlink in a parent directory of a target: the target itself is checked, its parents are not.
- Nothing proves you ran step 0. Targets outside the repository are out of scope, and paths with spaces are not supported.
