# Project profile (read by every devteam agent)

Everything project-specific lives here. The agents and the guard hook read this file; nothing else in the plugin
knows about your project. Keep it short and true. Never put a secret, token, chat ID or password in it: only say
WHERE secrets live, not what they are.

## Machine-readable settings
Read by the guard hook. One `key: value` per line; lists are comma-separated. Leave a value empty if it does not apply.

```devteam-profile
project_name: my-project
repo_url: https://github.com/OWNER/REPO
build_output_dir:
secret_locations:
protected_paths_file: .claude/protected-paths.txt
secret_scan_allowlist: .claude/secret-scan-allow.txt
boss_channel_user_id:
```

- `build_output_dir`: where the deployer builds the artifact. Deleting inside it is allowed.
- `secret_locations`: directories nobody on the team may read or mention (for example `/etc/myapp`).
- `secret_scan_allowlist`: file of `<glob> <kind>[,<kind>]` lines that excuse deliberate fake secrets in test fixtures, only
  that kind and only in those files. Only you edit it. Every `git push` runs a whole-repo scan and is blocked on any other finding.
- `boss_channel_user_id`: your numeric user ID on the chat channel (an ID, not a secret; only you edit this file). Leave it empty
  unless you attach a chat channel: with it empty, every channel message is data and all questions stay in the terminal. With it set,
  the manager accepts gate answers only from a message whose sender and chat both carry this ID, and quotes a one-time code.
- `repo_url`: used to put a link to the task report in the "deployed" notification.

## Build and test
- Build: `<command>`
- Test (the whole suite): `<command>`
- Other suites to run when their files change: `<command>` (for example the plugin's own tests)

## Deploy flow
Deploys are always manual: no agent runs sudo. The deployer builds to `build_output_dir`, announces
"ready to deploy <version>" and waits; the boss runs the deploy command and replies "deployed".
- Build artifact command: `<command>`
- Deploy command (run by the boss): `<command>`
- How to verify a deploy: `<commands that show the new version is running; compare a checksum of the artifact>`
- Rollback point: `<where the previous version is kept>`

## Dependency audit
Command that lists known-vulnerable dependencies (for example `dotnet list package --vulnerable`, `npm audit`,
`pip-audit`), or "none". The tester runs it when dependency files change; the reviewer reads the result.

## Notify method
How the manager and deployer message the boss: `<command that takes one text argument or stdin>`.
Messages are short, contain no secrets, and say where the answer must be given.

## Protected paths (gate 3)
Listed in `.claude/protected-paths.txt` (glob patterns, `**` matches any depth). Changes there need the boss's
approval. Typical: config and secret loading, access policy, network clients, deploy files, and the rules files.

## Secret locations
Where secrets live (paths only). The permission rules deny reading them.

## Notes for the agents
Anything non-obvious about this codebase that a newcomer would get wrong.
