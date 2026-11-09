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
   the notify method, which paths are security-sensitive, and (only if they attach a chat channel for approvals) their numeric
   user ID on it, which goes into `boss_channel_user_id`; otherwise leave it empty. If they give an ID, point them to the access check in the README ("Approvals through a chat channel") to see who the channel lets in.
3. Create `.claude/protected-paths.txt` from `${CLAUDE_PLUGIN_ROOT}/templates/protected-paths.txt` with your
   suggestions for this project, and ask the boss to confirm the list.
4. Create `docs/BACKLOG.md` from the BACKLOG template, `docs/reports/INDEX.md` from the INDEX template, and add
   `.team-work/` to `.gitignore`.
5. Create the project's own guard hook, so the guard (including the pre-push secret scan) also works where managed settings pin
   hooks with `allowManagedHooksOnly` and plugin hooks therefore do not run. Copy these two files together, unchanged, into the
   project's `.claude/hooks/` (create the folder): `${CLAUDE_PLUGIN_ROOT}/scripts/guard_bash.py` and
   `${CLAUDE_PLUGIN_ROOT}/scripts/secret_scan.py`, and make `guard_bash.py` executable. They must stay side by side: the hook
   imports the scanner from its own folder. Tell the boss these files are security code and ask them to read them once.
6. Print, for the boss to review and install themselves (they are security settings; do not write them):
   - the `hooks.PreToolUse` entry for `.claude/settings.json` (it is in `${CLAUDE_PLUGIN_ROOT}/templates/settings.json`):
     matcher `Bash`, command `${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_bash.py`, `onFailure: block`, timeout 30;
   - if the boss uses a root-owned managed settings file with `allowManagedHooksOnly: true`, the same hook must be pinned there with
     an absolute path (see `${CLAUDE_PLUGIN_ROOT}/templates/managed-settings.example.json`), otherwise it does not run;
   - the `.claude/settings.json` rules: start from `${CLAUDE_PLUGIN_ROOT}/templates/settings.json`, add one
     `Edit(/<pattern>)` ask rule per protected path, a `Read(//<secret path>/**)` deny per secret location, and the
     allow rules for the build and test commands from the profile;
   - optionally a root-owned managed settings file based on `${CLAUDE_PLUGIN_ROOT}/templates/managed-settings.example.json`
     so the agents cannot weaken the deny rules.
7. Run `${CLAUDE_PLUGIN_ROOT}/scripts/secret_scan.py` over the whole project. For findings that are deliberate fakes in test
   fixtures, propose narrow allow-list lines (`<glob> <kind>`) for the boss to review and install as
   `.claude/secret-scan-allow.txt`; anything else is a real finding to fix. Every `git push` runs this scan again.
8. Explain the gates the boss will be asked at (plan approval, production deploy, security-sensitive changes, and any push,
   share or upload, fix-round exception or review waiver)
   and that the next step is `/devteam:go <goal>`.

If a permission rule or a hook denies something while you do this, do not rewrite it another way. Stop and ask the boss.
