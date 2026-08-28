---
name: ai-worklog
description: Use when the user explicitly asks to bind the current Codex or Claude Code session to a requirement, ticket, project, task, or work-order ID, or to recall/query archived AI coding work in Obsidian.
license: Apache-2.0
metadata:
  author: 4Nz
  version: "0.1.0"
  compatibility: "macOS; Codex or Claude Code; Python 3.10+; Obsidian"
---

# AI Worklog

Run only after an explicit `$ai-worklog` (Codex) or `/ai-worklog:ai-worklog` (Claude Plugin) invocation. Parse the request into exactly one operation: `bind <work_item_id> <session_title...>`, `recall <work_item_id>`, or `query <search_text...>`. Ask for the missing choice if ambiguous.

Run `python3 scripts/ai_worklog.py ...` with the working directory set to this Skill directory. Pass every value as a separate, quoted argument and parse the single JSON object from stdout or stderr. The helper owns Agent/session detection, validation, archive access, and writes; never edit dossiers directly.

## Bind

1. Run `prepare-bind`; never infer an Agent or Session ID.

   ```bash
   python3 scripts/ai_worklog.py prepare-bind --work-item-id 'REQ-123' --session-title '幂等设计' --cwd '/absolute/project/path'
   ```

   Stop on every error. A binding conflict, malformed dossier, or unsupported/ambiguous Agent must leave both title and archive unchanged.
2. Derive topics, project role, result, next step, status, and the summary only from the visible conversation, verified repository facts, and `archive_summary`/`archive_sessions` from preparation. Build summary references as JSON objects, for example `{"agent_id":"codex","session_id":"..."}` or `{"agent_id":"claude-code","session_id":"..."}`. Do not use bare Session ID strings. Use `未知` for unsupported text and status.
3. Run `stage-bind` and inspect `agent_id`, `rename_mode`, `target_title`, and `manual_rename_command`:

   ```bash
   python3 scripts/ai_worklog.py stage-bind --prepared-token '/private/token' --topics-json '["幂等"]' --project-role '支付回调' --result '方案已确定' --next-step '增加测试' --status '进行中' --summary-json '{"current_progress":"方案已确定","unresolved":"增加测试","recommended_session":{"agent_id":"codex","session_id":"..."},"evidence_sessions":[{"agent_id":"codex","session_id":"..."}]}'
   ```

   Staging must not write the dossier.
4. For Codex `automatic` mode, call `set_thread_title` first with `target_title` and omit `threadId` so it renames the current task. When that tool is unavailable, run `python3 scripts/ai_worklog.py rename-thread --staged-token '/private/token'` to rename and verify through Codex app-server. If the available automatic path fails, ask the user to rename the task manually and wait for confirmation.
5. For Claude Code `manual` mode, show the returned `/rename ...` command and wait for explicit confirmation.
6. Only after confirmation, run `python3 scripts/ai_worklog.py commit-bind --staged-token '/private/token' --rename-confirmed`. A refusal or missing confirmation leaves the staged token and dossier unchanged.
7. A commit error after the rename is a partial bind. Report it and instruct the user to rerun the identical bind. A successful result with `warnings` persisted the dossier; report its cleanup warning without calling the bind a failure.

When the helper reports `error_code: user_action_required` with `choices`, ask the user to choose a vault, run `configure --vault-path '/chosen/vault'`, then rerun the interrupted operation. Only when it reports `action: enable_obsidian_cli`, ask before enabling/registering. If declined, rerun the interrupted command with `--filesystem-fallback` and include that flag on every remaining stage/commit command for this bind. If accepted, let the user enable it and rerun without the flag. Temporary CLI failures, including Obsidian not running, already use the scoped filesystem store and require no prompt or fallback flag. Never change Obsidian settings yourself.

## Recall

Run `python3 scripts/ai_worklog.py recall --work-item-id 'REQ-123'`. Present the summary, the deduplicated project list, then every session newest first. For every session show title, date/time, project, topics, result, next step, status, **Agent**, Session ID, and the returned resume command exactly as returned.

## Query

Run `python3 scripts/ai_worklog.py query --text '幂等'`. Search only `AI-Coding-Archive/WorkItems`; never broaden after zero results. Preserve the current work-item summary/project grouping and show every matched field and evidence. For every returned session, show **Agent**, Session ID, and the returned resume command. Keep summary-only evidence at work-item level; do not invent a session association. Ranking is unchanged.

## Claude Code Installation

Repository-root Plugin installation discovers this command as `/ai-worklog:ai-worklog`. The user must review and trust its synchronous SessionStart command hook before installing it. The hook supplies the Claude Code Agent and Session ID; without that hook identity the bind fails with `unsupported_agent`. Do not infer identity from a transcript, executable, directory, or config file.

Read [references/dossier-schema.md](references/dossier-schema.md) only when diagnosing malformed archive data or changing managed fields.
