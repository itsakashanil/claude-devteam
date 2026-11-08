# Project profile (read by every devteam agent)

Everything project-specific lives here. The agents and the guard hook read this file; nothing else in the plugin
knows about your project. Keep it short and true. Never put a secret, token, chat ID or password in it: only say
WHERE secrets live, not what they are.

## Machine-readable settings
Read by the guard hook. One `key: value` per line; lists are comma-separated. Leave a value empty if it does not apply.

```devteam-profile
project_name: my-project
repo_url: https://github.com/OWNER/REPO
deploy_command_path:
build_output_dir:
secret_locations:
protected_paths_file: .claude/protected-paths.txt
```

- `deploy_command_path`: the single absolute path of a root deploy script, if your deploys use sudo. The deployer
  agent is then the only one allowed to run exactly `sudo <path>`, and only after you approve. Empty = no sudo at all.
- `build_output_dir`: where the deployer builds the artifact. Deleting inside it is allowed.
- `secret_locations`: directories nobody on the team may read or mention (for example `/etc/myapp`).
- `repo_url`: used to put a link to the task report in the "deployed" notification.

## Build and test
- Build: `<command>`
- Test (the whole suite): `<command>`
- Other suites to run when their files change: `<command>` (for example the plugin's own tests)

## Deploy flow
Who runs the deploy and how. For a manual deploy: the deployer builds to `build_output_dir`, announces
"ready to deploy <version>" and waits; the boss runs the deploy command and replies "deployed".
- Build artifact command: `<command>`
- Deploy command (run by the boss, or by the deployer if sudo is configured): `<command>`
- How to verify a deploy: `<commands that show the new version is running; compare a checksum of the artifact>`
- Rollback point: `<where the previous version is kept>`

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
