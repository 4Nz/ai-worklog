from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.dossier import Dossier, DossierFormatError, SummaryFields, new_dossier  # noqa: E402
from worklog.model import AgentId, SessionRecord, SessionRef, ValidationError  # noqa: E402
from worklog.obsidian import RECORDS_FOLDER, SearchMatch  # noqa: E402
from worklog.operations import FIELD_RANK, DOSSIER_QUERY, NotFound, query, recall  # noqa: E402


OLD_SESSION_ID = "0191f8c0-7a11-7000-8000-000000000001"
NEW_SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"
NOW = "2026-08-27T10:00:00+08:00"


def record(
    session_id: str,
    occurred_at: str,
    project: str,
    *,
    agent_id: AgentId = "codex",
    title: str | None = None,
    topics: tuple[str, ...] = ("idempotency",),
    result: str = "design selected",
    next_step: str = "add tests",
) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id=session_id,
        title=title or f"REQ-123 {project} design",
        occurred_at=occurred_at,
        project_name=project,
        project_root=f"/work/{project}",
        repository=f"https://git.example.com/pay/{project}.git",
        topics=topics,
        result=result,
        next_step=next_step,
        status="进行中",
    )


def dossier_text(
    work_item_id: str,
    records: tuple[SessionRecord, ...],
    *,
    progress: str = "design selected",
    unresolved: str = "add tests",
    updated_at: str = NOW,
) -> str:
    dossier = Dossier.parse(new_dossier(work_item_id, NOW))
    for session in records:
        dossier.upsert(session)
    recommended = records[-1].ref if records else None
    return dossier.render(
        work_item_id,
        updated_at,
        SummaryFields(
            current_progress=progress,
            unresolved=unresolved,
            recommended_session=recommended,
            evidence_sessions=tuple(item.ref for item in records),
            project_roles={item.project_name: "owner" for item in records},
        ),
    )


class FakeStore:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temporary.name) / "vault"
        self.files: dict[Path, str] = {}
        self.searches: list[str] = []

    def close(self) -> None:
        self.temporary.cleanup()

    def add(self, filename: str, text: str) -> None:
        self.files[RECORDS_FOLDER / filename] = text

    def search(self, query: str) -> tuple[SearchMatch, ...]:
        self.searches.append(query)
        return tuple(
            SearchMatch(path, text)
            for path, text in self.files.items()
            if query in text
        )

    def read(self, relative_path: Path) -> str:
        return self.files[relative_path]

    def write(self, relative_path: Path, text: str) -> None:
        self.files[relative_path] = text


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.addCleanup(self.store.close)

    def test_recall_is_case_insensitive_exact_lookup_and_newest_first(self):
        old = record(
            OLD_SESSION_ID,
            "2026-08-27T09:30:00+08:00",
            "payment-api",
        )
        new = record(
            NEW_SESSION_ID,
            "2026-08-27T02:00:00-07:00",
            "order-service",
        )
        self.store.add("REQ-123.md", dossier_text("REQ-123", (old, new)))

        result = recall("req-123", self.store)

        self.assertEqual(result.work_item_id, "REQ-123")
        self.assertEqual(
            [session.session_id for session in result.sessions],
            [NEW_SESSION_ID, OLD_SESSION_ID],
        )
        self.assertEqual(result.projects, ("order-service", "payment-api"))
        self.assertEqual(result.summary.current_progress, "design selected")
        self.assertEqual(
            result.sessions[0].resume_command,
            f"codex resume {NEW_SESSION_ID}",
        )
        self.assertEqual(self.store.searches, [DOSSIER_QUERY])

    def test_recall_is_flat_newest_first_with_agent_attribution(self):
        old = record(
            "shared-id",
            "2026-08-27T09:30:00+08:00",
            "payment-api",
        )
        new = record(
            "shared-id",
            "2026-08-27T10:30:00+08:00",
            "order-service",
            agent_id="claude-code",
        )
        self.store.add("REQ-1.md", dossier_text("REQ-1", (old, new)))

        result = recall("REQ-1", self.store)

        self.assertEqual(
            [(item.agent_id, item.session_id) for item in result.sessions],
            [("claude-code", "shared-id"), ("codex", "shared-id")],
        )
        self.assertEqual(result.sessions[0].resume_command, "claude --resume shared-id")
        self.assertEqual(result.sessions[1].resume_command, "codex resume shared-id")
        self.assertEqual(
            result.summary.recommended_session,
            SessionRef("claude-code", "shared-id"),
        )
        self.assertEqual(
            result.summary.evidence_sessions,
            (SessionRef("codex", "shared-id"), SessionRef("claude-code", "shared-id")),
        )

    def test_recall_does_not_fall_back_to_fuzzy_search(self):
        self.store.add(
            "REQ-123.md",
            dossier_text(
                "REQ-123",
                (
                    record(
                        OLD_SESSION_ID,
                        "2026-08-27T09:30:00+08:00",
                        "payment-api",
                    ),
                ),
            ),
        )

        with self.assertRaises(NotFound):
            recall("REQ-12", self.store)

        self.assertEqual(self.store.searches, [DOSSIER_QUERY])

    def test_recall_rejects_invalid_id_and_casefolded_archive_collision(self):
        with self.assertRaises(ValidationError):
            recall("../REQ-123", self.store)

        self.store.add("REQ-123.md", dossier_text("REQ-123", ()))
        self.store.add("req-123.md", dossier_text("req-123", ()))
        with self.assertRaisesRegex(DossierFormatError, "duplicate"):
            recall("REQ-123", self.store)

    def test_recall_returns_exact_reversible_summary_fields(self):
        progress = "progress <value> | `literal`"
        unresolved = "aiw:SGVsbG8="
        self.store.add(
            "REQ-123.md",
            dossier_text(
                "REQ-123",
                (),
                progress=progress,
                unresolved=unresolved,
            ),
        )

        result = recall("REQ-123", self.store)

        self.assertEqual(result.summary.current_progress, progress)
        self.assertEqual(result.summary.unresolved, unresolved)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.addCleanup(self.store.close)

    def test_query_groups_matches_and_explains_all_session_evidence(self):
        session = record(
            NEW_SESSION_ID,
            "2026-08-27T10:00:00+08:00",
            "payment-api",
            title="REQ-123 payment-api callback design",
            topics=("幂等", "callback"),
            result="支付幂等设计完成",
        )
        self.store.add("REQ-123.md", dossier_text("REQ-123", (session,)))

        groups = query("幂等", self.store)

        self.assertEqual([group.work_item_id for group in groups], ["REQ-123"])
        match = groups[0].sessions[0]
        self.assertEqual(match.session_id, NEW_SESSION_ID)
        self.assertEqual(match.matched_fields, ("topics", "result"))
        self.assertEqual(match.evidence, ("幂等", "支付幂等设计完成"))
        self.assertEqual(match.project, "payment-api")
        self.assertEqual(match.rank, 2)
        self.assertEqual(
            match.resume_command,
            f"codex resume {NEW_SESSION_ID}",
        )
        self.assertEqual(self.store.searches, [DOSSIER_QUERY])

    def test_query_preserves_ranking_and_adds_agent_to_each_match(self):
        codex = record(
            "shared-id",
            "2026-08-27T09:30:00+08:00",
            "payment-api",
        )
        claude = record(
            "shared-id",
            "2026-08-27T10:30:00+08:00",
            "order-service",
            agent_id="claude-code",
        )
        self.store.add("REQ-1.md", dossier_text("REQ-1", (codex, claude)))

        groups = query("idempotency", self.store)

        self.assertEqual(groups[0].rank, FIELD_RANK["topics"])
        self.assertEqual(
            {(item.agent_id, item.session_id) for item in groups[0].sessions},
            {("codex", "shared-id"), ("claude-code", "shared-id")},
        )
        self.assertEqual(groups[0].sessions[0].resume_command, "claude --resume shared-id")
        self.assertEqual(groups[0].sessions[1].resume_command, "codex resume shared-id")

    def test_query_matches_qualified_summary_refs_at_work_item_level(self):
        codex = record(
            "shared-id",
            "2026-08-27T09:30:00+08:00",
            "payment-api",
        )
        claude = record(
            "shared-id",
            "2026-08-27T10:30:00+08:00",
            "order-service",
            agent_id="claude-code",
        )
        self.store.add("REQ-1.md", dossier_text("REQ-1", (codex, claude)))

        qualified = query("claude-code/shared-id", self.store)[0]

        self.assertEqual(qualified.sessions, ())
        self.assertEqual(
            qualified.work_item_evidence.evidence,
            ("claude-code/shared-id", "claude-code/shared-id"),
        )

    def test_query_deduplicates_same_session_and_ranks_session_id_first(self):
        session = record(
            NEW_SESSION_ID,
            "2026-08-27T10:00:00+08:00",
            "payment-api",
        )
        self.store.add("REQ-123.md", dossier_text("REQ-123", (session,)))

        groups = query(NEW_SESSION_ID, self.store)

        self.assertEqual(sum(len(group.sessions) for group in groups), 1)
        self.assertEqual(groups[0].sessions[0].rank, 0)
        self.assertEqual(groups[0].sessions[0].matched_fields, ("session_id",))

    def test_summary_only_match_is_not_attributed_to_session(self):
        session = record(
            NEW_SESSION_ID,
            "2026-08-27T10:00:00+08:00",
            "payment-api",
        )
        self.store.add(
            "REQ-123.md",
            dossier_text(
                "REQ-123",
                (session,),
                progress="跨系统联调",
                unresolved="none",
            ),
        )

        groups = query("跨系统联调", self.store)

        self.assertEqual(groups[0].sessions, ())
        self.assertEqual(groups[0].work_item_evidence.field, "summary")
        self.assertEqual(groups[0].work_item_evidence.evidence, ("跨系统联调",))
        self.assertEqual(groups[0].rank, 6)

    def test_query_ignores_manual_notes_and_uses_unicode_casefold(self):
        session = record(
            NEW_SESSION_ID,
            "2026-08-27T10:00:00+08:00",
            "payment-api",
            title="REQ-123 payment-api Straße rollout",
        )
        text = dossier_text("REQ-123", (session,)).replace(
            "此区域不会被 Skill 覆盖。",
            "manual-only-secret",
        )
        self.store.add("REQ-123.md", text)

        self.assertEqual(query("manual-only-secret", self.store), ())
        groups = query("STRASSE", self.store)
        self.assertEqual(groups[0].sessions[0].matched_fields, ("title",))

    def test_query_sorts_groups_by_best_rank_then_updated_instant(self):
        project_match = record(
            OLD_SESSION_ID,
            "2026-08-25T10:00:00+08:00",
            "needle-service",
            title="PROJECT callback design",
        )
        result_match_old = record(
            NEW_SESSION_ID,
            "2026-08-26T10:00:00+08:00",
            "payment-api",
            result="needle result",
        )
        third_id = "0191f8c0-7a11-7000-8000-000000000003"
        result_match_new = record(
            third_id,
            "2026-08-27T10:00:00+08:00",
            "order-service",
            result="needle result",
        )
        self.store.add(
            "PROJECT.md",
            dossier_text(
                "PROJECT",
                (project_match,),
                updated_at="2026-08-25T10:00:00+08:00",
            ),
        )
        self.store.add(
            "OLDER.md",
            dossier_text(
                "OLDER",
                (result_match_old,),
                updated_at="2026-08-27T04:00:00+08:00",
            ),
        )
        self.store.add(
            "NEWER.md",
            dossier_text(
                "NEWER",
                (result_match_new,),
                updated_at="2026-08-26T23:00:00-07:00",
            ),
        )

        groups = query("needle", self.store)

        self.assertEqual(
            [group.work_item_id for group in groups],
            ["PROJECT", "NEWER", "OLDER"],
        )
        self.assertEqual([group.rank for group in groups], [3, 4, 4])

    def test_query_rejects_empty_or_control_text(self):
        for invalid in ("", "   ", "line\nbreak", "hidden\u200btext"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                query(invalid, self.store)

    def test_query_ignores_candidates_outside_fixed_records_directory(self):
        session = record(
            NEW_SESSION_ID,
            "2026-08-27T10:00:00+08:00",
            "outside-service",
        )
        self.store.files[Path("unrelated.md")] = dossier_text(
            "OUTSIDE-1", (session,)
        )

        self.assertEqual(query("outside-service", self.store), ())

    def test_summary_query_matches_decoded_text_and_preserves_work_item_attribution(self):
        progress = "progress <value> | `literal`"
        unresolved = "aiw:SGVsbG8="
        self.store.add(
            "REQ-123.md",
            dossier_text(
                "REQ-123",
                (),
                progress=progress,
                unresolved=unresolved,
            ),
        )

        punctuation_match = query("<VALUE> | `LITERAL`", self.store)[0]
        prefix_match = query("aiw:SGVsbG8=", self.store)[0]

        self.assertEqual(punctuation_match.sessions, ())
        self.assertEqual(
            punctuation_match.work_item_evidence.evidence,
            (progress,),
        )
        self.assertEqual(prefix_match.sessions, ())
        self.assertEqual(
            prefix_match.work_item_evidence.evidence,
            (unresolved,),
        )

    def test_in_scope_invalid_frontmatter_fails_archive_scan_closed(self):
        malformed = new_dossier("REQ-1", NOW).replace(
            "work_item_id: REQ-1",
            "work_item_id: ../invalid",
        )
        self.store.add("malformed.md", malformed)

        with self.assertRaisesRegex(DossierFormatError, "work item ID"):
            query("missing", self.store)
        with self.assertRaisesRegex(DossierFormatError, "work item ID"):
            recall("REQ-404", self.store)


if __name__ == "__main__":
    unittest.main()
