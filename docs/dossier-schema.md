# Dossier Schema

AI Worklog owns Markdown files directly under:

```text
AI-Coding-Archive/WorkItems/<work_item_id>.md
```

The filename must match `work_item_id`. Release `0.1.0` supports only
`schema_version: 1`.

## Frontmatter

Managed frontmatter has exactly these fields:

```yaml
type: ai-work-item-history
schema_version: 1
work_item_id: T-123
created_at: 2026-08-28T10:00:00+08:00
updated_at: 2026-08-28T11:00:00+08:00
```

Unknown, duplicate, malformed, or mismatched managed fields cause a safe parse
failure. AI Worklog does not reinterpret a newer schema.

## Sections

Each dossier contains:

1. one managed global Summary;
2. one managed deduplicated Projects projection;
3. zero or more managed Session regions;
4. user-controlled Markdown outside managed regions.

Manual notes are preserved byte-for-byte during an update.

## Session Records

A session record contains:

- occurrence time and opaque Session ID in its heading;
- required `Agent` (`codex` or `claude-code`);
- session title and generated resume command;
- project name, absolute project root, and sanitized repository URL;
- topics, result, next step, and status.

Logical uniqueness is `(Agent, Session ID)`. The same raw Session ID may appear
once for each Agent. Rebinding the same composite identity to another work item
is rejected.

Session markers use a 64-character lowercase SHA-256 key calculated from:

```text
agent_id + NUL + UTF-8(session_id)
```

The parser recalculates the key and rejects tampering, duplicate regions,
missing fields, invalid resume commands, and duplicate composite identities.

## Summary References

Recommended and evidence sessions always include both Agent and Session ID.
Every reference must resolve to a session in the same dossier. The summary is
global across all projects and Agents.

## Encoding And Safety

Managed text uses deterministic encoding before Markdown insertion. Session IDs
remain opaque but cannot be empty, exceed 256 Unicode code points, or contain
control characters. Raw Session IDs never become filenames or marker keys.

Repository credentials are never persisted. Query and recall reject malformed
managed dossiers rather than returning partially trusted data.

## Schema Changes

A future schema change requires documented transformation fixtures, backup
before mutation, idempotence, complete-result validation, rollback guidance,
and an explicit mixed-version safety decision. Packaging and ordinary upgrades
do not migrate schema 1.
