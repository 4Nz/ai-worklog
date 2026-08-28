# Release Process

Git tags and GitHub Releases are the canonical release identity. Codex and
Claude Code manifests, the Claude marketplace entry, and Skill metadata carry
the same SemVer.

## Prepare

1. Choose the next Semantic Version.
2. Update `.codex-plugin/plugin.json`.
3. Update `.claude-plugin/plugin.json`.
4. Update `.claude-plugin/marketplace.json`.
5. Update `skills/ai-worklog/SKILL.md` metadata.
6. Add the release to `CHANGELOG.md`.
7. Document compatibility, upgrade, migration, and rollback behavior.

Do not change dossier `schema_version` only because plugin code changes.

## Validate

From a clean checkout:

```bash
python3 -m unittest discover -s skills/ai-worklog/tests -v
python3 scripts/validate_release.py
python3 scripts/package_release.py --output-dir dist
claude plugin validate .claude-plugin/plugin.json --strict
claude plugin validate . --strict
git diff --check
git status --short
```

The last command must be empty before tagging, apart from an intentionally
ignored `dist/` directory. Inspect the generated archive and confirm it contains
no internal plans, personal paths, real Session IDs, credentials, or test cache.

## Native Upgrade Verification

Use isolated temporary configuration directories and a temporary Git remote.
Install the previous version, publish the candidate commit to that temporary
remote, and exercise the documented commands.

Codex:

```bash
codex plugin marketplace upgrade ai-worklog
```

Claude Code:

```bash
claude plugin marketplace update ai-worklog
claude plugin update ai-worklog@ai-worklog
```

Confirm both plugin lists report the candidate version and that a new Agent
session discovers the Skill and Claude hook. Never run release verification
against a real Obsidian vault.

## Publish

Tag the validated commit and push the tag:

```bash
git tag -s v0.1.0 -m "AI Worklog v0.1.0"
git push origin v0.1.0
```

Create the GitHub Release from that exact tag and attach the validated source
archive. Release notes include compatibility, both upgrade command sections,
known limitations, schema impact, and rollback boundaries.

## Rollback

Each platform can pin its marketplace to an earlier tag independently. Code
rollback never downgrades Obsidian data. When a release changes dossier schema,
the release notes must provide a separate data rollback procedure or state that
downgrade is unsupported.
