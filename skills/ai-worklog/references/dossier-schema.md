# Dossier Schema

Read this reference only to diagnose malformed archive data or change managed fields. Public `bind`, `recall`, and `query` orchestration belongs in `SKILL.md`.

## Location And Frontmatter

Each dossier is the direct child `AI-Coding-Archive/WorkItems/<work_item_id>.md`; filename spelling must exactly match frontmatter. Frontmatter has exactly `type: ai-work-item-history`, `schema_version: 1`, `work_item_id`, `created_at`, and `updated_at`, with no duplicates or extras. `schema_version` remains `1`; this is not a migration format.

## Managed Markers And Ownership

A marker is one complete line matching this exact regex:

```regex
^<!-- ai-worklog:(summary|projects|session:([0-9a-f]{64})):(start|end) -->$
```

There is exactly one paired `summary` region and one paired `projects` region. Each session has a paired `session:<64-lowercase-hex>` region. Regions are paired, non-overlapping, and unique; any other `<!-- ai-worklog:` token is malformed. The marker key is `sha256(agent_id UTF-8 + NUL byte + session_id UTF-8)`.

Text outside managed marker spans is unmanaged and is never a model edit target. In particular, preserve `人工备注` exactly. The helper alone may update `updated_at`, replace validated managed regions, and insert a new managed session before an exact line-anchored unmanaged `## 人工备注` heading. It validates the complete result before any write.

## Sessions And References

Each session record has required `Agent`, `session_id`, `title`, `occurred_at`, `project_name`, `project_root`, `repository`, `topics`, `result`, `next_step`, and `status` fields. Registered Agent values are exactly `codex` and `claude-code`; `Agent` is a Session attribute, not a dossier attribute.

Session IDs are opaque non-empty strings up to 256 characters with no Unicode control characters. A record is uniquely identified by the composite `(agent_id, session_id)`: the same raw ID is valid for two different Agents, but the same composite is unique within a dossier and cannot be bound to a different dossier. Cross-dossier checks reject that conflict before a prepare token is written.

The session heading projects as `### <occurred_at> · <session_id>` and the first literal ` · ` separates the two fields, so later occurrences remain part of the opaque Session ID.

`SessionRef` projects as `agent_id/session_id`. The whole projected value uses `aiw:` plus padded URL-safe Base64 of its UTF-8 bytes whenever it requires escaping, including reserved prefix `aiw:`, Markdown-sensitive characters, control characters, or the summary list delimiter. Decode only valid tagged values; malformed tagged text remains literal.

The Markdown record has `Agent`, `会话标题`, `快速恢复`, `项目`, `项目根目录`, `仓库`, `话题`, `讨论结果`, `下一步`, and `状态` exactly once. `快速恢复` is generated only from the registered Agent's trusted argv (`codex resume <id>` or `claude --resume <id>`), shell-quoted for display. Parsing regenerates and compares that command, rejecting a mismatched Agent, Session ID, or resume command.

## Summary And Projects

The global summary fields are `规模`, `当前进展`, `未决事项`, `建议恢复`, and `摘要依据`. Recommendation and evidence use `SessionRef` values and must identify records in the same dossier. The project table has `项目`, `仓库`, and `在工作项中的作用`, with one case-insensitively unique row per project. This global summary/project model is unchanged.

Managed scalar values beginning with `aiw:` or containing `<`, `|`, backticks, newlines, or control characters use `aiw:` plus padded URL-safe Base64 of their UTF-8 bytes. `话题` uses `无` for an empty tuple; a literal `无` topic or one containing `、` is encoded before projection. Project-table text is single-line: line breaks/control bytes become spaces, `<` becomes `\u003c`, and pipes/backticks are backslash-escaped.
