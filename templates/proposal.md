# Proposal: <name>

Use this for a change the boss must install by hand: files under `.claude/`, `CLAUDE.md`, or anything the agents may
not edit. Deliver the files and this README under `docs/proposed/<name>/`. Replace every `<...>`.

## Why
<The problem, in two or three lines.>

## What changes
| Source (docs/proposed/<name>/) | Target | New or changed |
|---|---|---|
| <file> | <target path> | <one line> |

## What the checks cannot see
<Keep this honest: what the tests and the review do not cover, and which risks stay with the boss.>

## Install (the boss, from the repo root, in this order)
`<REVIEWED_COMMIT>` is the full hash the manager gives the boss at hand-over (this README cannot hold its own commit's
hash). `<BASE_COMMIT>` is the commit the targets were last reviewed at. Any output where "prints nothing" is stated, or
any non-zero exit, means stop and tell the manager.

0. Verify the source is the reviewed commit, before any copy:
   ```
   git branch --show-current                                         # must print task/<id>-<slug>
   git status --porcelain -- docs/proposed/<name>                    # must print nothing
   git diff --exit-code <REVIEWED_COMMIT> -- docs/proposed/<name>    # must exit 0 and print nothing
   ```
1. Verify the targets have not drifted from the base:
   ```
   git diff --exit-code <BASE_COMMIT> -- <targets>
   ```
2. Review each file:
   ```
   diff -u <target> docs/proposed/<name>/<file>
   ```
3. Install:
   ```
   install -m 0644 docs/proposed/<name>/<file> <target>
   ```
4. Check. Run the relevant tests with every test-only override variable unset:
   ```
   env -u <OVERRIDE_VAR> <test command>
   ```
   Expected: <N> tests, all passing.
5. Rollback if step 4 fails:
   ```
   git checkout <BASE_COMMIT> -- <targets>
   ```
6. After a good install: restart the session if agent definitions changed; commit the installed files on the task
   branch; <who merges to main>; remove `docs/proposed/<name>/`.
