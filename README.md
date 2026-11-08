# claude-devteam

A small AI dev team for any repository, as a Claude Code plugin. You give it a goal; a **manager** turns it into tasks and runs each
one through **plan -> build -> test (3 runs) -> review -> hand-over to deploy**, using a **planner**, **builder**, **tester**,
**reviewer** and **deployer**. You are asked at exactly three gates, and every finished task gets a written report.

Status: v0.2.0, MIT licensed. Built for one project (a home-lab bot) and generalised; expect rough edges.

## What you get
| Piece | What it does |
|---|---|
| `/devteam:init` | Sets the team up in a project: profile, protected paths, backlog, reports folder, and the permission rules for you to install. |
| `/devteam:go <goal>` | Gives the manager a goal. It runs the whole pipeline and asks you only at the gates. |
| `/devteam:status` | Summarises `docs/BACKLOG.md`: in progress, blocked, waiting for you, agent-run budget. |
| 6 agents | manager, planner, builder, tester (sonnet), reviewer (read-only, must PASS), deployer. Opus plans and reviews; sonnet does routine work. |
| Guard hook | A `PreToolUse` hook for Bash: no sudo except the one deploy command, deletes only inside the repo, no force pushes, no writes to `.claude/`, a prompt before shell edits of protected paths, and project secret locations are never mentioned. |
| `scripts/secret_scan.py` | Scans every tracked and new file, including `docs/reports/`, for tokens, keys, chat IDs and email addresses. Prints where, never what. |
| Templates | Project profile, report, backlog, reports index, protected paths, settings and managed-settings examples. |

## The three gates
1. **Plan approval**: nothing is built until you approve the planner's plan.
2. **Production deploy**: the deployer builds and says "ready to deploy <version>". By default **you** run the deploy and reply "deployed";
   the deployer then verifies that the running artifact is the one you approved and sends you a summary with a link to the report.
3. **Security-sensitive code**: edits to the paths you list in `.claude/protected-paths.txt` raise a permission prompt for you.

The manager cannot approve a gate, overrule a failed review, change `CLAUDE.md`, `.claude/` or any permission setting. A task gets at
most 2 fix rounds, then it is escalated to you. If a rule or hook denies something, agents stop and ask you; they do not route around it.

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

## The project profile
Everything project-specific lives in `.claude/project-profile.md` (template: `templates/project-profile.md`): build and test commands,
the deploy flow, the notify method, protected paths, secret locations (paths only, never secrets), and the repository URL. The agents read it
every time; the guard hook reads its fenced `devteam-profile` block. Without a profile the hook applies its strictest generic rules.

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
  If you set `allowManagedHooksOnly`, plugin hooks stop running unless you also pin this plugin's hook there.
- **Deploying is yours.** No agent can run sudo (the hook blocks it for everyone, the deployer included). The deployer builds, announces
  "ready to deploy <version>" and, after you deploy, verifies the running artifact. It also refuses to build if anything other than the backlog
  or reports changed since the commit the reviewer saw. It cannot prove the build tooling itself is uncompromised.
- **Pushes are scanned.** Every `git push` through the Bash tool first runs a whole-repo secret scan and is blocked on any finding. Deliberate
  fakes in test fixtures are excused by `.claude/secret-scan-allow.txt` (`<glob> <kind>` per line, only that kind, only those files; an entry
  with no kind is ignored). Only you edit that file.
- **Approvals are narrow.** An approval after a denial covers only the exact action you approved.
- **One task at a time per repository.** Two manager sessions in one repo are not supported.
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
