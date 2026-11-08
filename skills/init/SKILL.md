---
description: Set up the dev team in this project. Creates .claude/project-profile.md, protected-paths, the backlog and the reports folder, and prints the permission rules to install. Run once per project.
disable-model-invocation: true
---

Set up the devteam plugin in the current project. Be careful and explain what you create. Do not overwrite anything
that already exists: if a file is there, show the boss the difference and ask.

1. Look at the project (read-only first): language and build tool, how tests are run, whether there is a deploy
   script, where secrets live (paths only; never open or print a secret), and the git remote URL.
2. Create `.claude/project-profile.md` from `${CLAUDE_PLUGIN_ROOT}/templates/project-profile.md`, filled in from what
   you found. Leave unknown values empty and say so. Ask the boss for anything you cannot find out: the deploy command,
   the notify method, which paths are security-sensitive.
3. Create `.claude/protected-paths.txt` from `${CLAUDE_PLUGIN_ROOT}/templates/protected-paths.txt` with your
   suggestions for this project, and ask the boss to confirm the list.
4. Create `docs/BACKLOG.md` from the BACKLOG template, `docs/reports/INDEX.md` from the INDEX template, and add
   `.team-work/` to `.gitignore`.
5. Print, for the boss to review and install themselves (they are security settings; do not write them):
   - `.claude/settings.json` rules: start from `${CLAUDE_PLUGIN_ROOT}/templates/settings.json`, add one
     `Edit(/<pattern>)` ask rule per protected path, a `Read(//<secret path>/**)` deny per secret location, and the
     allow rules for the build and test commands from the profile;
   - optionally a root-owned managed settings file based on `${CLAUDE_PLUGIN_ROOT}/templates/managed-settings.example.json`
     so the agents cannot weaken the deny rules.
6. Run `${CLAUDE_PLUGIN_ROOT}/scripts/secret_scan.py` over the whole project. For findings that are deliberate fakes in test
   fixtures, propose narrow allow-list lines (`<glob> <kind>`) for the boss to review and install as
   `.claude/secret-scan-allow.txt`; anything else is a real finding to fix. Every `git push` runs this scan again.
7. Explain the three gates the boss will be asked at (plan approval, production deploy, security-sensitive changes)
   and that the next step is `/devteam:go <goal>`.

If a permission rule or a hook denies something while you do this, do not rewrite it another way. Stop and ask the boss.
