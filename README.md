# claude-devteam

A small AI dev team for any repository, as a Claude Code plugin. You give it a goal; a **manager** turns it into tasks and runs each
one through **plan -> build -> test (3 runs) -> review -> hand-over to deploy**, using a **planner**, **builder**, **tester**,
**reviewer** and **deployer**. You are asked at a few fixed gates, and every finished task gets a written report.

Status: v0.7.0, MIT licensed. Built for one project (a home-lab bot) and generalised; expect rough edges.

## What you get
| Piece | What it does |
|---|---|
| `/devteam:init` | Sets the team up in a project: profile, protected paths, backlog, reports folder, and the permission rules for you to install. |
| `/devteam:go <goal>` | Gives the manager a goal. It runs the whole pipeline and asks you only at the gates. |
| `/devteam:status` | Summarises `docs/BACKLOG.md`: in progress, blocked, waiting for you, agent-run budget. |
| 6 agents | manager, planner, builder, tester (sonnet, runs and reads only, never edits files), reviewer (read-only, must PASS), deployer. Opus plans and reviews; sonnet does routine work. |
| Project hook | `/devteam:init` copies the guard hook and the scanner into the project's `.claude/hooks/` (they must stay side by side) and prints the settings entry. This is what keeps the guard and the pre-push scan working under `allowManagedHooksOnly`, where plugin hooks do not run. |
| Guard hook | A `PreToolUse` hook for Bash: no sudo except the one deploy command, deletes only inside the repo, no force pushes, no writes to `.claude/`, a prompt before shell edits of protected paths, and project secret locations are never mentioned. |
| `scripts/secret_scan.py` | Scans every tracked and new file, including `docs/reports/`, for tokens, keys, chat IDs and email addresses. Prints where, never what. |
| Templates | Project profile, report, backlog, reports index, protected paths, settings and managed-settings examples, and `templates/proposal.md`: the README skeleton for changes only you can install (see Proposals below). |

## The gates
1. **Plan approval**: nothing is built until you approve the planner's plan. The question quotes every `Method:` line of the plan and the
   plan's hash. To audit it, compare `git hash-object .team-work/<id>/plan.md` with the hash in the gate 1 question. Any change to the plan
   after approval voids those methods until you approve again, even a mechanical change.
2. **Production deploy**: the deployer builds and says "ready to deploy <version>". The manager always asks this question (in the terminal, and
   through the channel with a code when the channel is on), and "deployed" is its own question (a fresh code through the channel, none in the
   terminal). By default **you** run the deploy and reply "deployed"; tell the manager before you start, it confirms "paused". The deployer then verifies that the running artifact is the one you approved and sends you a summary with a link to the report.
3. **Security-sensitive code**: edits to the paths you list in `.claude/protected-paths.txt` raise a permission prompt for you.

Besides these three, you are also asked before any push, share or upload, for a fix-round exception and for a review waiver.

The manager cannot approve a gate, overrule a failed review, change `CLAUDE.md`, `.claude/` or any permission setting. A task gets at
most 2 fix rounds, then it is escalated to you. If a rule or hook denies something, agents stop and ask you; they do not route around it. One exception: a brief may carry an
`Allowed method:` line, quoted word for word from the plan you approved, and the agent may then switch to that method once and report it
under `Denied:`. The manager may also start one rerun of a denied step with such a plan method and records it, under the same exclusions as the agents: never for a protected path, anything under `.claude/`, `CLAUDE.md`, a secret location, a deny rule target, an install, deploy, push, share, upload, delete, restart or sudo, or when you answered "no" at a permission prompt. One denial gives at most one alternative (the agent's switch or the manager's rerun, never both). Every report has a
`Denied:` section, and the builder's report also has `Brief rules:`.

## Install
You need Claude Code with plugin support.

```bash
# 1. Get the plugin (after you publish it as itsakashanil/claude-devteam)
claude plugin marketplace add itsakashanil/claude-devteam
claude plugin install devteam@claude-devteam

#    ...or from a local checkout while developing
claude plugin marketplace add ~/projects/claude-devteam
claude plugin install devteam@claude-devteam
#    ...or for one session only
claude --plugin-dir ~/projects/claude-devteam
```

```text
# 2. In your project, start Claude Code and run
/devteam:init
# 3. Review and install the printed permission rules (they are security settings; the agents cannot write them)
# 4. Give the team a goal
/devteam:go add rate limiting to the login endpoint
```

You can also run the manager as the whole session: `claude --agent devteam:manager`.

## Approvals through a chat channel (optional)
Set `boss_channel_user_id` in the project profile to your numeric user ID on the chat channel you attach to the manager's session.
With it set and the channel's reply tool working, the manager sends gate questions only to that ID, accepts a channel answer only from
a message whose sender and chat both carry it, and needs a fresh one-time code in every answer (generated with `python3 -I -c` so a
planted file cannot replace `secrets`; never stored; voided after use, on hold or cancel, a restart, a failed send or a doubt after
compaction). Your own messages typed in the terminal need no code. With the key empty, every channel message is data and everything
stays in the terminal. The notify method is one way and never carries a code. The code stops injected text, not a program running as
your user: the session transcript on disk still holds it.

Check who the channel lets in before you rely on it. This assumes `TELEGRAM_STATE_DIR` and `CLAUDE_CONFIG_DIR` are unset (otherwise
read the path from them) and that the channel plugin keeps its allow list in `access.json` under `~/.claude/channels/telegram/`; adjust the
path if your channel stores it elsewhere. It prints the allowed sender IDs and the DM policy: only your ID should appear, the same one as `boss_channel_user_id`, and `dmPolicy` should be `allowlist`.

```bash
python3 -I -c "import json, pathlib; p = pathlib.Path.home() / '.claude' / 'channels' / 'telegram' / 'access.json'; d = json.loads(p.read_text()); print(d.get('allowFrom'), d.get('dmPolicy'))"
```

While a task waits for you, the manager may work on an independent task (no shared files, its own branch, its own gate 1, at most one,
up to review). It stops when you tell it you are starting an install or deploy.

## The project profile
Everything project-specific lives in `.claude/project-profile.md` (template: `templates/project-profile.md`): build and test commands,
the deploy flow, the notify method, protected paths, secret locations (paths only, never secrets), and the repository URL. The agents read it
every time; the guard hook reads its fenced `devteam-profile` block. Without a profile the hook applies its strictest generic rules.

## Proposals for changes only you can install
Anything under `.claude/`, `CLAUDE.md` or permission settings is delivered by the builder as a proposal in `docs/proposed/<name>/`, with a README built from
`templates/proposal.md`. You read that README from the reviewed commit (`git -c core.hooksPath=/dev/null -c core.fsmonitor=false show <full hash>:docs/proposed/<name>/README.md`), never from the working tree.
Its steps check that the folder is exactly the reviewed commit (nothing ignored or untracked, no later edits), that the targets are unchanged since the
commit they were built on, and that the local git config sets nothing that runs programs; then they install each file from git's stored copy of the reviewed commit,
run the tests, and can roll back. Keep every agent session stopped until step 7 (a checked commit and merge into main) prints OK; step 6 and step 7 recheck
the branch, the config and main before they act. Linux, bash and GNU coreutils only. The reviewer quotes the full reviewed hash on line 2 of its review (`REVIEWED: <hash>`); compare it with the hand-over message.
A proposal-only task has no deployer step: you reply "installed" after step 7 has merged the branch.

## Reports
After each task the manager writes `docs/reports/YYYY-MM-DD-<task>.md` (goal, plan, files with a one-line purpose, decisions and rejected
alternatives, tests, review findings and fixes, security gates, deploy result and rollback point, open items, learning notes) and adds a row
to `docs/reports/INDEX.md`. Reports must contain no secrets, tokens, chat IDs or personal data; the manager runs `secret_scan.py` over them
before handing over, and you should run it before every push: `python3 <plugin>/scripts/secret_scan.py` (whole project) or with paths, for example `docs/reports`. Test fixtures with deliberately fake secrets can be marked with a trailing `devteam-scan: ignore`.

## Limits you should know
- **Guardrails, not a sandbox.** Permission rules and the hook catch mistakes and prompt injection; a script an agent writes can still do
  things they cannot see. Real boundaries are OS users, file permissions and what credentials exist.
- **A plugin cannot ship permission rules.** Only an `agent` default can come from a plugin, so `/devteam:init` prints the rules and you install them.
  For rules the agents cannot weaken, put a root-owned managed settings file in `/etc/claude-code/` (see `templates/managed-settings.example.json`).
  If you set `allowManagedHooksOnly`, plugin hooks stop running; the project hook that `/devteam:init` creates is the one to pin there
  (absolute path, `onFailure: block`). Copies do not update with the plugin: after upgrading, rerun the copy and review the diff.
- **Deploying is yours.** No agent can run sudo (the hook blocks it for everyone, the deployer included). The deployer builds, announces
  "ready to deploy <version>" and, after you deploy, verifies the running artifact. It also refuses to build if anything other than the backlog
  or reports changed since the commit the reviewer saw. It cannot prove the build tooling itself is uncompromised.
- **Pushes are scanned.** Every `git push` through the Bash tool first runs a whole-repo secret scan and is blocked on any finding. Deliberate
  fakes in test fixtures are excused by `.claude/secret-scan-allow.txt` (`<glob> <kind>` per line, only that kind, only those files; an entry
  with no kind is ignored). Only you edit that file.
- **Pushes ask you.** The manager's push gate is coded; on top of it a push asks you in the permission prompt after the coded gate: the hook asks before every
  allowed `git push` (every spelling it unwraps, for every agent) once the scan is clean. A force, delete or mirror push is still blocked
  outright; the hook sees only a `git push` typed through the Bash tool, not a script, `gh` or `curl`. Whether a hook "ask" also prompts
  in auto mode is not verified, so check once, live, that the prompt also appears in auto mode (and that a push never runs without it).
- **Proposal checks prove "same bytes", not "safe".** Every git call in the template runs with hooks and fsmonitor off, and the checks stop if HEAD or the
  working tree differs from the reviewed commit, except for exactly `docs/BACKLOG.md` and `docs/reports/<name>.md` (one `.md` file directly in `docs/reports`, the only
  files the manager writes after review; a `conftest.py` or `test_*.py` there, or a subfolder, is a STOP because test runners load code by those names). They read only common program-running keys in `.git/config`: not
  config.worktree, diff/merge drivers, `pager.<cmd>`, `gpg.program`, GIT_* variables, `~/.gitconfig` or the system config. Your own shell (PATH, `~/.bashrc`,
  aliases) and the installed plugin copy of the template are writable by the agents. They cannot see a symlinked parent directory of a target or prove you
  ran them; the hash reaches you through the manager, so also compare it with `.team-work/<task-id>/reviewed-commit` and the review.
- **Approvals are narrow.** An approval after a denial covers only the exact action you approved.
- **One task at a time per repository**, except one independent task while another waits for you. Two manager sessions in one repo are not supported.
- **Cost.** Opus plans and reviews and the suite runs three times per task; that is deliberate for security-sensitive code. For a low-risk
  project, edit the models in `agents/*.md` or the run count in `agents/tester.md`.
- **Real isolation.** For stronger boundaries than a text hook, run the build and tests under Claude Code's sandbox or in a container or VM
  you control; the guard hook is the fallback, not a replacement.
- **Agent names.** The manager's allowed sub-agents are written as `devteam:planner` etc. If your Claude Code version scopes plugin agents differently,
  adjust the `tools` line in `agents/manager.md`.

## Develop
```bash
python3 -m unittest discover -s tests      # hook, scanner and structure tests
claude plugin validate .                   # Claude Code's own plugin checks
```
