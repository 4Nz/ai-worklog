# Architecture

AI Worklog has one shared Python core with thin Agent-specific identity and
rename adapters. Codex and Claude Code load the same `skills/ai-worklog`
directory from separate native plugin packages.

## Components

```text
Codex environment ------> Codex adapter -----+
                                             |
Claude SessionStart hook -> Claude adapter ---+-> bind/recall/query core
                                                     |
                                                     v
                                             Obsidian work-item dossier
```

- `worklog/agent.py` detects exactly one registered Agent and produces an
  immutable session identity.
- `worklog/adapters/` owns trusted resume commands and platform rename behavior.
- `worklog/operations.py` implements prepare, stage, commit, recall, and query.
- `worklog/dossier.py` parses and renders only managed Markdown regions.
- `worklog/obsidian.py` selects the vault and provides official CLI or scoped
  filesystem persistence.
- `worklog/locking.py` serializes local writers that share a vault.
- `hooks/claude_session_start.py` exports Claude's current session identity; it
  does not rename sessions or access Obsidian.

## Identity

A session is identified by `(agent_id, session_id)`. Session IDs are opaque and
are never used directly as paths or marker keys. Managed marker identity is the
SHA-256 digest of `agent_id + NUL + UTF-8(session_id)`.

Adapters generate resume arguments from trusted code:

```text
codex resume <session-id>
claude --resume <session-id>
```

The display command is shell-quoted. Stored commands are regenerated and
validated while parsing.

## Bind State Machine

```text
prepare -> stage -> rename gate -> commit -> read-back verification
```

Prepare validates Agent identity, project identity, vault selection, current
dossier structure, and cross-work-item binding conflicts without changing the
task or archive. Stage validates model-derived summary fields and writes only a
short-lived local capability token.

Codex attempts automatic rename. Claude Code asks the user to run its returned
`/rename` command. Commit is allowed only after automatic success or explicit
manual confirmation. It revalidates all captured state under a local advisory
lock, writes the dossier, and reads it back before reporting success.

## Persistence Boundary

Configuration and locks live under `~/.config/ai-worklog`. Dossiers live only
under `AI-Coding-Archive/WorkItems` in the selected Obsidian vault. Query never
searches outside that managed directory.

The official Obsidian CLI is preferred when usable. A scoped filesystem store
provides atomic replacement when CLI use is unavailable or declined. Both paths
perform read-back verification. Local locking does not provide distributed
coordination between separate machines syncing one vault.

## Version Boundaries

Plugin SemVer describes executable code and packaging. Dossier
`schema_version` describes persisted data. They change independently. Codex and
Claude Code may temporarily run different plugin releases, so releases that
retain one schema must remain safe for the previous supported minor release.

See [Dossier schema](dossier-schema.md) for the persisted contract.
