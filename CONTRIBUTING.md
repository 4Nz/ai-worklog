# Contributing

Contributions are welcome through GitHub issues and pull requests.

## Development Requirements

- Python 3.10 or later
- macOS for platform integration changes
- Codex and/or Claude Code for native plugin validation
- Obsidian for manual integration testing

Runtime modules under `skills/ai-worklog/scripts` must use only the Python
standard library. Development tooling may add dependencies only when CI pins and
documents them.

## Workflow

1. Open an issue for behavior or schema changes.
2. Add a failing test that demonstrates the requested behavior.
3. Implement the smallest compatible change.
4. Run the complete verification suite.
5. Update English and Chinese documentation when user-facing behavior changes.
6. Add a changelog entry.

```bash
python3 -m unittest discover -s skills/ai-worklog/tests -v
python3 scripts/validate_release.py
claude plugin validate .claude-plugin/plugin.json --strict
claude plugin validate . --strict
git diff --check
```

Do not add real Vault contents, Session IDs, absolute project paths, credentials,
or private repository URLs to fixtures, logs, issues, or commits.

## Schema Changes

A dossier schema proposal must include old and new fixtures, an idempotent
transformation, backup and rollback behavior, mixed Codex/Claude version safety,
and proof that unmanaged notes remain untouched. Incrementing plugin SemVer does
not by itself justify a schema change.

## Pull Requests

Keep changes scoped, explain compatibility impact, include verification output,
and identify any operation that was not tested. Apache-2.0 applies to submitted
contributions; no separate contributor license agreement is required.
