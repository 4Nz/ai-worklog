from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.dossier import (  # noqa: E402
    Dossier,
    DossierFormatError,
    PersistenceError,
    SummaryFields,
    atomic_write_verified,
    find_binding,
    new_dossier,
    resolve_work_item,
)
from worklog.model import SessionRecord, SessionRef, record_key  # noqa: E402


SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"
SECOND_SESSION_ID = "0191f8c0-7a11-7000-8000-000000000001"
TITLE = "REQ-1 payment-api idempotency"
NOW = "2026-08-27T11:30:00+08:00"
REF = SessionRef("codex", SESSION_ID)
SECOND_REF = SessionRef("codex", SECOND_SESSION_ID)


def record(result: str = "first", **changes: object) -> SessionRecord:
    values: dict[str, object] = {
        "agent_id": "codex",
        "session_id": SESSION_ID,
        "title": TITLE,
        "occurred_at": "2026-08-27T10:00:00+08:00",
        "project_name": "payment-api",
        "project_root": "/work/payment-api",
        "repository": "https://git.example.com/pay/payment-api.git",
        "topics": ("idempotency", "payment callback"),
        "result": result,
        "next_step": "add unique index",
        "status": "进行中",
    }
    values.update(changes)
    return SessionRecord(**values)  # type: ignore[arg-type]


def summary(**changes: object) -> SummaryFields:
    values: dict[str, object] = {
        "current_progress": "design selected",
        "unresolved": "add duplicate-message test",
        "recommended_session": REF,
        "evidence_sessions": (REF,),
        "project_roles": {"payment-api": "handles callbacks"},
    }
    values.update(changes)
    return SummaryFields(**values)  # type: ignore[arg-type]


def fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def write_dossier(path: Path, session_id: str = SESSION_ID, work_item_id: str = "REQ-1") -> None:
    dossier = Dossier.parse(new_dossier(work_item_id, "2026-08-27T10:00:00+08:00"))
    dossier.upsert(record(session_id=session_id))
    path.write_text(dossier.render(work_item_id, NOW, summary()), encoding="utf-8")


class DossierTests(unittest.TestCase):
    def test_parse_rejects_overlapping_or_unclosed_markers(self):
        malformed = "<!-- ai-worklog:summary:start -->\n<!-- ai-worklog:projects:start -->"
        with self.assertRaisesRegex(DossierFormatError, "managed marker"):
            Dossier.parse(malformed)

    def test_parse_rejects_malformed_managed_marker(self):
        malformed = new_dossier("REQ-1", NOW) + "<!-- ai-worklog:summary:finish -->\n"
        with self.assertRaisesRegex(DossierFormatError, "managed marker"):
            Dossier.parse(malformed)

    def test_upsert_refreshes_one_session_without_duplication(self):
        dossier = Dossier.parse(new_dossier("REQ-1", "2026-08-27T10:00:00+08:00"))
        dossier.upsert(record(result="first"))
        dossier.upsert(record(result="updated"))
        rendered = dossier.render("REQ-1", NOW, summary())
        self.assertEqual(rendered.count(f"ai-worklog:session:{record_key(REF)}:start"), 1)
        self.assertIn("**讨论结果**：updated", rendered)

    def test_second_session_ignores_manual_heading_text_inside_managed_value(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record(result="see ## 人工备注 section"))
        first = dossier.render("REQ-1", NOW, summary())
        refreshed = Dossier.parse(first)
        refreshed.upsert(
            record(
                session_id=SECOND_SESSION_ID,
                title="REQ-1 payment-api follow-up",
                occurred_at="2026-08-27T11:00:00+08:00",
                result="follow-up complete",
            )
        )

        rendered = refreshed.render(
            "REQ-1",
            NOW,
            summary(
                recommended_session=SECOND_REF,
                evidence_sessions=(REF, SECOND_REF),
            ),
        )
        reparsed = Dossier.parse(rendered)

        self.assertEqual(set(reparsed.records), {REF, SECOND_REF})
        self.assertIn("**讨论结果**：see ## 人工备注 section", rendered)
        self.assertLess(
            rendered.index(f"session:{record_key(REF)}:end"),
            rendered.index(f"session:{record_key(SECOND_REF)}:start"),
        )
        self.assertLess(
            rendered.index(f"session:{record_key(SECOND_REF)}:end"),
            rendered.index("\n## 人工备注\n"),
        )

    def test_render_preserves_manual_notes_exactly(self):
        original = fixture("dossier_with_manual_notes.md")
        manual = original.split("## 人工备注", 1)[1]
        rendered = Dossier.parse(original).render("REQ-1", NOW, summary())
        self.assertEqual(rendered.split("## 人工备注", 1)[1], manual)

    def test_render_recomputes_counts_and_validates_summary_references(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())
        rendered = dossier.render("REQ-1", NOW, summary())
        self.assertIn("涉及 1 个项目，共 1 条会话", rendered)
        with self.assertRaisesRegex(DossierFormatError, "summary reference"):
            dossier.render(
                "REQ-1", NOW, summary(recommended_session=SessionRef("codex", "missing"))
            )
        with self.assertRaisesRegex(DossierFormatError, "summary reference"):
            dossier.render(
                "REQ-1", NOW, summary(evidence_sessions=(SessionRef("codex", "missing"),))
            )

    def test_same_raw_id_under_two_agents_is_unambiguous(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        codex = record(agent_id="codex", session_id="shared/raw id")
        claude = record(
            agent_id="claude-code",
            session_id="shared/raw id",
            title="REQ-1 claude",
        )
        dossier.upsert(codex)
        dossier.upsert(claude)
        refs = (
            SessionRef("codex", "shared/raw id"),
            SessionRef("claude-code", "shared/raw id"),
        )
        rendered = dossier.render(
            "REQ-1",
            NOW,
            summary(recommended_session=refs[1], evidence_sessions=refs),
        )
        parsed = Dossier.parse(rendered)
        self.assertEqual(set(parsed.records), set(refs))
        self.assertIn("- **Agent**：codex", rendered)
        self.assertIn("- **Agent**：claude-code", rendered)
        self.assertIn("claude --resume 'shared/raw id'", rendered)

    def test_session_id_containing_heading_delimiter_round_trips(self):
        session_id = "opaque · still-session-id"
        original = record(session_id=session_id)
        ref = SessionRef("codex", session_id)
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)

        rendered = dossier.render(
            "REQ-1",
            NOW,
            summary(recommended_session=ref, evidence_sessions=(ref,)),
        )
        parsed = Dossier.parse(rendered)

        self.assertEqual(parsed.records[ref], original)

    def test_marker_is_hash_and_tampering_fails_closed(self):
        original = record(agent_id="claude-code", session_id="unsafe/../id")
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render(
            "REQ-1",
            NOW,
            summary(
                recommended_session=original.ref,
                evidence_sessions=(original.ref,),
            ),
        )
        key = record_key(original.ref)
        self.assertIn(f"session:{key}:start", rendered)
        self.assertNotIn("session:unsafe/../id", rendered)
        with self.assertRaisesRegex(DossierFormatError, "session marker"):
            Dossier.parse(rendered.replace(key, "0" * 64))

    def test_summary_reference_containing_list_delimiter_round_trips(self):
        original = record(agent_id="claude-code", session_id="part、two")
        ref = original.ref
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render(
            "REQ-1", NOW, summary(recommended_session=ref, evidence_sessions=(ref,))
        )
        parsed = Dossier.parse(rendered)
        self.assertEqual(parsed.summary.recommended_session, ref)
        self.assertEqual(parsed.summary.evidence_sessions, (ref,))

    def test_parse_rejects_duplicate_summary_labels(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())
        rendered = dossier.render("REQ-1", NOW, summary())
        duplicates = (
            ("当前进展", "shadowed progress"),
            ("未决事项", "shadowed unresolved item"),
            ("建议恢复", "`codex/missing`"),
            ("摘要依据", "`codex/missing`"),
        )
        for label, duplicate_value in duplicates:
            with self.subTest(label=label):
                original_line = next(
                    line
                    for line in rendered.splitlines()
                    if line.startswith(f"> - **{label}**：")
                )
                corrupted = rendered.replace(
                    original_line,
                    f"{original_line}\n> - **{label}**：{duplicate_value}",
                    1,
                )
                with self.assertRaisesRegex(DossierFormatError, "summary projection"):
                    Dossier.parse(corrupted)

    def test_parse_rejects_composite_session_corruption(self):
        original = record(agent_id="claude-code", session_id="part、two")
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render(
            "REQ-1",
            NOW,
            summary(
                recommended_session=original.ref,
                evidence_sessions=(original.ref,),
            ),
        )
        key = record_key(original.ref)
        session_start = rendered.index(f"<!-- ai-worklog:session:{key}:start -->")
        session_end = (
            rendered.index(f"<!-- ai-worklog:session:{key}:end -->")
            + len(f"<!-- ai-worklog:session:{key}:end -->")
        )
        duplicate = (
            rendered[:session_end]
            + "\n\n"
            + rendered[session_start:session_end]
            + rendered[session_end:]
        )
        corruptions = (
            (rendered.replace("- **Agent**：claude-code\n", ""), "session content"),
            (rendered.replace("- **Agent**：claude-code", "- **Agent**：unknown-agent"), "session reference"),
            (rendered.replace("claude --resume 'part、two'", "codex resume part"), "session content"),
            (duplicate, "duplicate session marker"),
            (
                rendered.replace(
                    "`aiw:Y2xhdWRlLWNvZGUvcGFydOOAgXR3bw==`",
                    "`codex/missing`",
                ),
                "summary reference",
            ),
        )
        for corrupted, category in corruptions:
            with self.subTest(category=category):
                with self.assertRaisesRegex(DossierFormatError, category):
                    Dossier.parse(corrupted)

    def test_parse_exposes_rendered_project_roles(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())
        rendered = dossier.render(
            "REQ-1",
            NOW,
            summary(project_roles={"payment-api": "callback | owner"}),
        )
        self.assertEqual(
            Dossier.parse(rendered).project_roles,
            {"payment-api": "callback | owner"},
        )

    def test_model_text_cannot_inject_managed_markers_or_headings(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record(result="line one\n<!-- ai-worklog:summary:start -->\n## injected"))
        rendered = dossier.render("REQ-1", NOW, summary())
        self.assertEqual(rendered.count("<!-- ai-worklog:summary:start -->"), 1)
        self.assertNotIn("<!-- ai-worklog:summary:start --> ## injected", rendered)

    def test_structured_fields_round_trip_without_rewriting_valid_characters(self):
        original = record(
            title="title <value> | `literal`",
            project_name="payment|api",
            project_root="/work/<payment>`api",
            repository="https://git.example.com/pay/<api>|`main`",
            topics=("<topic>", "pipe|topic", "`topic`"),
            result="result <value> | `literal`",
            next_step="next <value> | `literal`",
        )
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render("REQ-1", NOW, summary())
        reparsed = Dossier.parse(rendered)
        self.assertEqual(reparsed.records[REF], original)
        self.assertEqual(reparsed.render("REQ-1", NOW, summary()), rendered)

    def test_atomic_write_accepts_valid_title_with_marker_characters(self):
        original = record(title="title <value> | `literal`")
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render("REQ-1", NOW, summary())
        with tempfile.TemporaryDirectory() as temporary:
            atomic_write_verified(Path(temporary) / "REQ-1.md", rendered, REF, original.title)

    def test_reserved_encoding_tag_payload_round_trips_and_verifies(self):
        original = record(title="aiw:SGVsbG8=")
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(original)
        rendered = dossier.render("REQ-1", NOW, summary())
        reparsed = Dossier.parse(rendered)
        self.assertEqual(reparsed.records[REF], original)
        self.assertEqual(reparsed.render("REQ-1", NOW, summary()), rendered)
        with tempfile.TemporaryDirectory() as temporary:
            atomic_write_verified(Path(temporary) / "REQ-1.md", rendered, REF, original.title)

    def test_summary_fields_round_trip_reserved_characters_and_encoding_prefix(self):
        original = summary(
            current_progress="progress <value> | `literal`",
            unresolved="aiw:SGVsbG8=",
        )
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())

        rendered = dossier.render("REQ-1", NOW, original)
        reparsed = Dossier.parse(rendered)

        self.assertEqual(reparsed.summary, original)
        self.assertNotIn(
            "> - **未决事项**：aiw:SGVsbG8=",
            rendered,
        )
        self.assertEqual(reparsed.render("REQ-1", NOW, reparsed.summary), rendered)

    def test_parse_rejects_invalid_frontmatter_work_item_ids(self):
        for invalid in (
            "../invalid",
            "REQ 1",
            "中文-1",
            "REQ/1",
            "A" * 65,
        ):
            with self.subTest(invalid=invalid):
                malformed = new_dossier("REQ-1", NOW).replace(
                    "work_item_id: REQ-1",
                    f"work_item_id: {invalid}",
                )
                with self.assertRaisesRegex(DossierFormatError, "work item ID"):
                    Dossier.parse(malformed)

    def test_parse_requires_exact_supported_frontmatter(self):
        valid = new_dossier("REQ-1", NOW)
        malformed_values = (
            valid.replace("type: ai-work-item-history", "type: other"),
            valid.replace("schema_version: 1", "schema_version: 2"),
            valid.replace(
                "type: ai-work-item-history",
                "type: ai-work-item-history\ntype: ai-work-item-history",
            ),
            valid.replace("work_item_id: REQ-1", "extra: value\nwork_item_id: REQ-1"),
            valid.replace("created_at: " + NOW + "\n", ""),
            valid.replace("updated_at: " + NOW, "updated_at: 2026-08-27T11:30:00"),
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed.splitlines()[:8]):
                with self.assertRaisesRegex(DossierFormatError, "frontmatter"):
                    Dossier.parse(malformed)

    def test_parse_requires_marker_integrity_and_timezone_aware_timestamp(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())
        rendered = dossier.render("REQ-1", NOW, summary())
        malformed_id = rendered.replace(record_key(REF), "0" * 64)
        malformed_time = rendered.replace(
            "### 2026-08-27T10:00:00+08:00",
            "### 2026-08-27T10:00:00",
        )

        for malformed in (malformed_id, malformed_time):
            with self.subTest():
                with self.assertRaises(DossierFormatError):
                    Dossier.parse(malformed)

    def test_parse_rejects_invalid_session_projection_contract(self):
        dossier = Dossier.parse(new_dossier("REQ-1", NOW))
        dossier.upsert(record())
        rendered = dossier.render("REQ-1", NOW, summary())
        malformed_values = (
            rendered.replace("- **状态**：进行中", "- **状态**：done"),
            rendered.replace("`/work/payment-api`", "`relative/payment-api`"),
            rendered.replace(
                f"`codex resume {SESSION_ID}`",
                f"`codex resume {SECOND_SESSION_ID}`",
            ),
            rendered.replace(
                "https://git.example.com/pay/payment-api.git",
                "https://user:secret@git.example.com/pay/payment-api.git",
            ),
            rendered.replace(
                "- **状态**：进行中",
                "- **状态**：进行中\n- **状态**：进行中",
            ),
        )
        for malformed in malformed_values:
            with self.subTest():
                with self.assertRaisesRegex(DossierFormatError, "session"):
                    Dossier.parse(malformed)


class DossierFilesystemTests(unittest.TestCase):
    def test_find_binding_is_exact_for_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_dossier(root / "REQ-1.md")
            binding = find_binding(root, REF)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.work_item_id, "REQ-1")
            self.assertEqual(binding.path, root / "REQ-1.md")
            self.assertIsNone(find_binding(root, SessionRef("codex", SESSION_ID.upper())))

    def test_find_binding_rejects_global_casefolded_work_item_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_dossier(root / "REQ-1.md")
            write_dossier(root / "duplicate.md", work_item_id="req-1")
            with self.assertRaisesRegex(DossierFormatError, "duplicate case-folded"):
                find_binding(root, REF)

    def test_resolve_work_item_reuses_existing_case_spelling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_dossier(root / "REQ-1.md")
            self.assertEqual(resolve_work_item(root, "req-1"), root / "REQ-1.md")

    def test_resolve_work_item_rejects_duplicate_casefolded_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_dossier(root / "REQ-1.md")
            write_dossier(root / "duplicate.md", work_item_id="req-1")
            with self.assertRaisesRegex(DossierFormatError, "duplicate"):
                resolve_work_item(root, "REQ-1")

    def test_atomic_write_verifies_session_and_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "REQ-1.md"
            valid_text = Dossier.parse(new_dossier("REQ-1", NOW))
            valid_text.upsert(record())
            rendered = valid_text.render("REQ-1", NOW, summary())
            with mock.patch("pathlib.Path.read_text", return_value="corrupt"):
                with self.assertRaisesRegex(PersistenceError, "verification"):
                    atomic_write_verified(path, rendered, REF, TITLE)
