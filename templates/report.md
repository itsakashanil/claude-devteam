# Report: <task title>

- Task: <T-id>
- Date: <YYYY-MM-DD>
- Branch and commits: <branch>, <short hashes>
- Status: <deployed | merged, not deployed | dropped | blocked>

## Goal
<what the boss asked for, in their words, and what changes for the user>

## Plan
<the approved plan in a few lines; note any amendment and why>

## Files
| File | Created or changed | Purpose |
|---|---|---|
| `path/to/file` | created | <one line> |

## Decisions and rejected alternatives
- Chose <X> because <reason>. Rejected <Y>: <why>.

## Tests
<tests added by the builder, the 3 runs (passed/failed/skipped each), gaps found>

## Review
<verdict, then each finding with severity, what was done about it, or why it was not>

## Security gates
- Gate 1 (plan): <approved by the boss on date>
- Gate 3 (security-sensitive code): <not touched | files and approval>
- Gate 2 (deploy): <ready-to-deploy build, checksum prefix, deployed by the boss on date>

## Deploy result and rollback point
<verification result: active, checksum matches, log findings; where the previous version is kept>

## Open items
- <anything left, with who owns it>

## Learning notes
1. <a short, concrete lesson from this task>
2. <another>
