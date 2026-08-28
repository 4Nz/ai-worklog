from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.dossier import (  # noqa: E402
    Dossier,
    DossierFormatError,
    SummaryFields,
    new_dossier,
)
from worklog.model import SessionRecord, SessionRef, record_key  # noqa: E402
from worklog.locking import WorkItemBusy  # noqa: E402
from worklog.obsidian import StoreResolution  # noqa: E402
import worklog.operations as operations  # noqa: E402
from worklog.operations import (  # noqa: E402
    BindRequest,
    BindingConflict,
    DerivedFields,
    PartialBindFailure,
    RenameRequired,
    StaleDossier,
    SummaryInput,
    TokenError,
    commit_bind,
    prepare_bind,
    stage_bind,
)


SESSION_ID = "01a040e7-4f68-7fd0-8804-588374eaa690"
ENV = {"CODEX_SESSION_ID": SESSION_ID}
NOW = "2026-08-27T10:00:00+08:00"
DOSSIER = Path("AI-Coding-Archive/WorkItems/REQ-123.md")
SECOND_SESSION_ID = "0191f8c0-7a11-7000-8000-000000000001"
THIRD_SESSION_ID = "0191f8c0-7a11-7000-8000-000000000002"
REF = SessionRef("codex", SESSION_ID)
SECOND_REF = SessionRef("codex", SECOND_SESSION_ID)
THIRD_REF = SessionRef("codex", THIRD_SESSION_ID)


class DelayedFilesystemStore:
    def __init__(self, vault_path: str):
        self._store = StoreResolution(
            Path(vault_path),
            "filesystem",
            lambda args, cwd: subprocess.CompletedProcess(args, 1, "", ""),
        )
        self.vault_path = self._store.vault_path

    def search(self, query: str):
        matches = self._store.search(query)
        time.sleep(0.25)
        return matches

    def read(self, relative_path: Path) -> str:
        return self._store.read(relative_path)

    def write(self, relative_path: Path, text: str) -> None:
        self._store.write(relative_path, text)


def commit_in_process(
    staged_token: str,
    vault_path: str,
    token_root: str,
    lock_root: str,
    work_item_id: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait(5)
    try:
        commit_bind(
            staged_token,
            DelayedFilesystemStore(vault_path),
            env=ENV,
            now=NOW,
            token_root=Path(token_root),
            rename_confirmed=True,
            lock_root=Path(lock_root),
        )
    except PartialBindFailure as exc:
        results.put(("failure", work_item_id, exc.cause_error_code))
    except Exception as exc:
        results.put(("unexpected", work_item_id, type(exc).__name__))
    else:
        results.put(("success", work_item_id, None))


class FakeRunner:
    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command == ("git", "rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, "/work/payment-api\n", "")
        if command == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(
                args, 0, "https://git.example.com/pay/payment-api.git\n", ""
            )
        return subprocess.CompletedProcess(args, 1, "", "")


class SecondProjectRunner:
    def __call__(
        self, args: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command == ("git", "rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, "/work/order-service\n", "")
        if command == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(
                args, 0, "https://git.example.com/order/order-service.git\n", ""
            )
        return subprocess.CompletedProcess(args, 1, "", "")


class FakeStore:
    def __init__(self, vault_path: Path):
        self.vault_path = vault_path.resolve()
        self.files: dict[Path, str] = {}
        self.writes: list[tuple[Path, str]] = []

    def add(self, relative_path: Path, text: str) -> None:
        self.files[relative_path] = text

    def search(self, query: str):
        from worklog.obsidian import SearchMatch

        return tuple(
            SearchMatch(path, text)
            for path, text in self.files.items()
            if query in text
        )

    def read(self, relative_path: Path) -> str:
        return self.files[relative_path]

    def write(self, relative_path: Path, text: str) -> None:
        self.writes.append((relative_path, text))
        self.files[relative_path] = text

    @property
    def paths(self) -> set[Path]:
        return set(self.files)


class OneReadbackFailureStore(FakeStore):
    def __init__(self, vault_path: Path):
        super().__init__(vault_path)
        self.failures_remaining = 1
        self.fail_next_read = False

    def write(self, relative_path: Path, text: str) -> None:
        super().write(relative_path, text)
        if self.failures_remaining:
            self.fail_next_read = True

    def read(self, relative_path: Path) -> str:
        if self.fail_next_read:
            self.fail_next_read = False
            self.failures_remaining -= 1
            return "corrupt readback"
        return super().read(relative_path)


def dossier_with(
    work_item_id: str, session_id: str, *, agent_id: str = "codex"
) -> str:
    dossier = Dossier.parse(new_dossier(work_item_id, NOW))
    ref = SessionRef(agent_id, session_id)  # type: ignore[arg-type]
    dossier.upsert(
        SessionRecord(
            agent_id=ref.agent_id,
            session_id=session_id,
            title=f"{work_item_id} old title",
            occurred_at=NOW,
            project_name="payment-api",
            project_root="/work/payment-api",
            repository="https://git.example.com/pay/payment-api.git",
            topics=("idempotency",),
            result="old result",
            next_step="old next step",
            status="进行中",
        )
    )
    return dossier.render(
        work_item_id,
        NOW,
        SummaryFields("old", "none", ref, (ref,), {}),
    )


def derived_fields(**changes: object) -> DerivedFields:
    values: dict[str, object] = {
        "topics": ["idempotency", "payment callback", "idempotency"],
        "project_role": "handles callbacks",
        "result": "design selected",
        "next_step": "add unique index",
        "status": "进行中",
        "summary": SummaryInput(
            current_progress="design selected",
            unresolved="add duplicate-message test",
            recommended_session=REF,
            evidence_sessions=(REF,),
        ),
    }
    values.update(changes)
    return DerivedFields(**values)  # type: ignore[arg-type]


def tamper_token(path: str, mutate) -> None:
    token_path = Path(path)
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    mutate(payload["value"])
    token_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(token_path, 0o600)


class BindTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = FakeStore(self.root / "vault")
        self.request = BindRequest(
            work_item_id="REQ-123",
            session_title="幂等设计",
            cwd=Path("/work/payment-api/src"),
        )

    def prepare(self, now: str = NOW):
        return prepare_bind(
            self.request,
            env=ENV,
            store=self.store,
            now=now,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )

    def stage(self, fields: DerivedFields | None = None, now: str = NOW):
        prepared = self.prepare(now)
        return stage_bind(
            prepared.prepared_token,
            fields or derived_fields(),
            self.store,
            env=ENV,
            now=now,
            token_root=self.root / "tokens",
        )

    def commit(self, staged, now: str = NOW):
        return commit_bind(
            staged.staged_token,
            self.store,
            env=ENV,
            now=now,
            token_root=self.root / "tokens",
            rename_confirmed=True,
            lock_root=self.root / "locks",
        )


class PrepareBindTests(BindTestBase):

    def test_prepare_token_mode_is_0600_even_with_restrictive_umask(self):
        token_root = self.root / "tokens"
        token_root.mkdir()
        previous_umask = os.umask(0o777)
        try:
            result = prepare_bind(
                self.request,
                env=ENV,
                store=self.store,
                now=NOW,
                run=FakeRunner(),
                token_root=token_root,
            )
        finally:
            os.umask(previous_umask)
        self.assertEqual(os.stat(result.prepared_token).st_mode & 0o777, 0o600)

    def test_prepare_checks_everything_without_writing_or_renaming(self):
        result = prepare_bind(
            self.request,
            env=ENV,
            store=self.store,
            now=NOW,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )
        self.assertEqual(result.target_title, "REQ-123 幂等设计")
        self.assertEqual(result.agent_id, "codex")
        self.assertEqual(result.session_id, SESSION_ID)
        self.assertEqual(result.project.project_name, "payment-api")
        self.assertFalse(self.store.writes)
        self.assertEqual(os.stat(result.prepared_token).st_mode & 0o777, 0o600)

    def test_token_path_is_hashed_and_tokens_carry_agent_identity(self):
        env = {
            "AI_WORKLOG_AGENT": "claude-code",
            "AI_WORKLOG_SESSION_ID": "unsafe/../opaque id",
        }
        result = prepare_bind(
            self.request,
            env=env,
            store=self.store,
            now=NOW,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )
        token = Path(result.prepared_token)
        self.assertEqual(
            token.parent.name,
            record_key(SessionRef("claude-code", "unsafe/../opaque id")),
        )
        self.assertNotIn("unsafe", str(token.parent))
        payload = json.loads(token.read_text(encoding="utf-8"))["value"]
        self.assertEqual(
            (payload["agent_id"], payload["session_id"]),
            ("claude-code", "unsafe/../opaque id"),
        )

    def test_same_raw_id_different_agents_can_bind_same_item(self):
        self.store.add(
            DOSSIER, dossier_with("REQ-123", "shared", agent_id="codex")
        )
        env = {
            "AI_WORKLOG_AGENT": "claude-code",
            "AI_WORKLOG_SESSION_ID": "shared",
        }
        prepared = prepare_bind(
            self.request,
            env=env,
            store=self.store,
            now=NOW,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )
        self.assertIsNone(prepared.existing_record)
        self.assertEqual(prepared.agent_id, "claude-code")

    def test_same_composite_id_on_another_item_is_a_conflict(self):
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER.md"),
            dossier_with("OTHER", SESSION_ID, agent_id="codex"),
        )
        with self.assertRaises(BindingConflict) as caught:
            self.prepare()
        self.assertEqual(caught.exception.error_code, "binding_conflict")

    def test_non_bind_archive_scan_rejects_duplicate_composite_refs(self):
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER-1.md"),
            dossier_with("OTHER-1", SECOND_SESSION_ID),
        )
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER-2.md"),
            dossier_with("OTHER-2", SECOND_SESSION_ID),
        )

        with self.assertRaisesRegex(
            DossierFormatError, "session ID appears in multiple dossiers"
        ):
            operations.recall("OTHER-1", self.store)

    def test_prepare_does_not_duplicate_project_in_user_session_title(self):
        result = prepare_bind(
            BindRequest(
                work_item_id="REQ-123",
                session_title="payment-api 幂等设计",
                cwd=self.request.cwd,
            ),
            env=ENV,
            store=self.store,
            now=NOW,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )

        self.assertEqual(result.target_title, "REQ-123 payment-api 幂等设计")
        self.assertNotIn("payment-api payment-api", result.target_title)

    def test_cross_item_conflict_aborts_without_prepare_token(self):
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER-9.md"),
            dossier_with("OTHER-9", SESSION_ID),
        )
        with self.assertRaisesRegex(BindingConflict, "OTHER-9"):
            prepare_bind(
                self.request,
                env=ENV,
                store=self.store,
                now=NOW,
                run=FakeRunner(),
                token_root=self.root / "tokens",
            )
        self.assertFalse(self.store.writes)
        self.assertFalse((self.root / "tokens").exists())

    def test_prepare_rejects_noncanonical_dossier_filename_before_token(self):
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/req-123.md"),
            dossier_with("REQ-123", SECOND_SESSION_ID),
        )

        with self.assertRaisesRegex(ValueError, "path"):
            self.prepare()

        self.assertFalse(self.store.writes)
        self.assertFalse((self.root / "tokens").exists())

    def test_prepare_returns_all_archive_evidence_without_manual_notes(self):
        dossier = Dossier.parse(new_dossier("REQ-123", NOW))
        dossier.upsert(
            SessionRecord(
                agent_id="codex",
                session_id=SECOND_SESSION_ID,
                title="REQ-123 discovery",
                occurred_at="2026-08-27T09:00:00+08:00",
                project_name="payment-api",
                project_root="/work/payment-api",
                repository="https://git.example.com/pay/payment-api.git",
                topics=("discovery",),
                result="mapped flow",
                next_step="design fix",
                status="已完成",
            )
        )
        dossier.upsert(
            SessionRecord(
                agent_id="codex",
                session_id=THIRD_SESSION_ID,
                title="REQ-123 design",
                occurred_at="2026-08-27T09:30:00+08:00",
                project_name="payment-api",
                project_root="/work/payment-api",
                repository="https://git.example.com/pay/payment-api.git",
                topics=("design",),
                result="selected design",
                next_step="implement",
                status="进行中",
            )
        )
        text = dossier.render(
            "REQ-123",
            NOW,
            SummaryFields(
                "selected design",
                "implement",
                THIRD_REF,
                (SECOND_REF, THIRD_REF),
                {"payment-api": "owner"},
            ),
        ).replace("此区域不会被 Skill 覆盖。", "private manual note")
        self.store.add(DOSSIER, text)

        prepared = self.prepare()

        self.assertIsNone(prepared.existing_record)
        self.assertEqual(prepared.archive_summary.current_progress, "selected design")
        self.assertEqual(
            [record.session_id for record in prepared.archive_sessions],
            [THIRD_SESSION_ID, SECOND_SESSION_ID],
        )
        self.assertNotIn("private manual note", repr(prepared))

    def test_token_write_and_consume_fsync_directories_in_order(self):
        events: list[tuple[str, str]] = []
        descriptor_paths: dict[int, str] = {}
        real_open = os.open
        real_fsync = os.fsync
        real_unlink = Path.unlink
        fsync_probe = self.root / "fsync-probe"
        fsync_probe.touch()

        def tracked_open(path, flags, *args):
            opened_path = fsync_probe if Path(path).is_dir() else path
            descriptor = real_open(opened_path, flags, *args)
            descriptor_paths[descriptor] = str(path)
            events.append(("open", str(path)))
            return descriptor

        def tracked_fsync(descriptor):
            events.append(("fsync", descriptor_paths.get(descriptor, "token-file")))
            return real_fsync(descriptor)

        def tracked_unlink(path, *args, **kwargs):
            events.append(("unlink", str(path)))
            return real_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(operations.os, "open", side_effect=tracked_open),
            mock.patch.object(operations.os, "fsync", side_effect=tracked_fsync),
            mock.patch.object(Path, "unlink", tracked_unlink),
        ):
            prepared = self.prepare()
            events.clear()
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )

        staged_file_sync = next(
            index
            for index, event in enumerate(events)
            if event[0] == "fsync" and "staged-" in event[1]
        )
        predecessor_unlink = next(
            index
            for index, event in enumerate(events)
            if event[0] == "unlink" and "prepared-" in event[1]
        )
        directory_syncs = [
            index
            for index, event in enumerate(events)
            if event[0] == "fsync" and event[1].endswith(record_key(REF))
        ]
        self.assertLess(staged_file_sync, directory_syncs[0])
        self.assertLess(directory_syncs[0], predecessor_unlink)
        self.assertGreater(directory_syncs[-1], predecessor_unlink)


class StagedBindTests(BindTestBase):
    def test_stage_returns_adapter_rename_metadata(self):
        env = {
            "AI_WORKLOG_AGENT": "claude-code",
            "AI_WORKLOG_SESSION_ID": "claude-1",
        }
        prepared = prepare_bind(
            self.request,
            env=env,
            store=self.store,
            now=NOW,
            run=FakeRunner(),
            token_root=self.root / "tokens",
        )
        fields = derived_fields(
            summary=SummaryInput(
                current_progress="design selected",
                unresolved="add tests",
                recommended_session=SessionRef("claude-code", "claude-1"),
                evidence_sessions=(SessionRef("claude-code", "claude-1"),),
            )
        )
        staged = stage_bind(
            prepared.prepared_token,
            fields,
            self.store,
            env=env,
            now=NOW,
            token_root=self.root / "tokens",
        )
        self.assertEqual(staged.agent_id, "claude-code")
        self.assertEqual(staged.rename_mode, "manual")
        self.assertEqual(
            staged.manual_rename_command, "/rename REQ-123 幂等设计"
        )

    def test_codex_stage_returns_automatic_rename_metadata(self):
        staged = self.stage()

        self.assertEqual(staged.agent_id, "codex")
        self.assertEqual(staged.rename_mode, "automatic")
        self.assertIsNone(staged.manual_rename_command)

    def test_stage_rejects_runtime_agent_change_with_same_raw_id(self):
        prepared = self.prepare()
        changed_agent_env = {
            "AI_WORKLOG_AGENT": "claude-code",
            "AI_WORKLOG_SESSION_ID": SESSION_ID,
        }

        with self.assertRaisesRegex(TokenError, "Agent or Session ID") as caught:
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=changed_agent_env,
                now=NOW,
                token_root=self.root / "tokens",
            )
        self.assertEqual(caught.exception.error_code, "token_error")

    def test_stage_summary_refs_require_exact_composite_identity(self):
        self.store.add(
            DOSSIER, dossier_with("REQ-123", SECOND_SESSION_ID, agent_id="codex")
        )
        fields = derived_fields(
            summary=SummaryInput(
                "progress",
                "none",
                SessionRef("claude-code", SECOND_SESSION_ID),
                (REF,),
            )
        )

        with self.assertRaisesRegex(ValueError, "recommended session"):
            stage_bind(
                self.prepare().prepared_token,
                fields,
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )

    def test_stage_validates_fields_deduplicates_topics_and_consumes_prepare(self):
        prepared = self.prepare()
        staged = stage_bind(
            prepared.prepared_token,
            derived_fields(result="", next_step=" ", project_role=""),
            self.store,
            env=ENV,
            now=NOW,
            token_root=self.root / "tokens",
        )
        self.assertEqual(staged.target_title, "REQ-123 幂等设计")
        self.assertFalse(Path(prepared.prepared_token).exists())
        self.assertEqual(os.stat(staged.staged_token).st_mode & 0o777, 0o600)
        self.assertFalse(self.store.writes)

    def test_stage_rejects_prepared_title_with_injected_project(self):
        prepared = self.prepare()
        tamper_token(
            prepared.prepared_token,
            lambda value: value.update(
                target_title="REQ-123 payment-api 幂等设计"
            ),
        )

        with self.assertRaisesRegex(TokenError, "invalid"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )

    def test_stage_reports_invalid_prepared_agent_as_token_error(self):
        prepared = self.prepare()
        tamper_token(
            prepared.prepared_token,
            lambda value: value.update(agent_id="unknown-agent"),
        )

        with self.assertRaises(TokenError) as caught:
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )
        self.assertEqual(caught.exception.error_code, "token_error")

    def test_stage_rejects_noncanonical_prepared_session_title(self):
        prepared = self.prepare()

        def add_outer_whitespace(value):
            value["session_title"] = " 幂等设计 "
            value["target_title"] = "REQ-123  幂等设计 "

        tamper_token(prepared.prepared_token, add_outer_whitespace)
        with self.assertRaisesRegex(TokenError, "invalid"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )

    def test_stage_rejects_invalid_topics_status_and_summary_evidence(self):
        invalid = (
            derived_fields(topics=["valid", "bad\nvalue"]),
            derived_fields(status="done"),
            derived_fields(
                summary=SummaryInput(
                    "progress", "none", None, (SessionRef("codex", "missing"),)
                )
            ),
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                prepared = self.prepare()
                with self.assertRaises(ValueError):
                    stage_bind(
                        prepared.prepared_token,
                        fields,
                        self.store,
                        env=ENV,
                        now=NOW,
                        token_root=self.root / "tokens",
                    )
                self.assertTrue(Path(prepared.prepared_token).exists())
                Path(prepared.prepared_token).unlink()

    def test_stage_rejects_expired_token_and_changed_runtime_session(self):
        prepared = self.prepare()
        with self.assertRaisesRegex(TokenError, "expired"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now="2026-08-27T10:11:00+08:00",
                token_root=self.root / "tokens",
            )
        other_env = {"CODEX_SESSION_ID": SECOND_SESSION_ID}
        with self.assertRaisesRegex(TokenError, "Session ID"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=other_env,
                now=NOW,
                token_root=self.root / "tokens",
            )

    def test_stage_rejects_extended_and_future_prepared_tokens(self):
        prepared = self.prepare()
        tamper_token(
            prepared.prepared_token,
            lambda value: value.update(expires_at="2026-08-27T10:11:00+08:00"),
        )
        with self.assertRaisesRegex(TokenError, "invalid"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )

        prepared = self.prepare()
        tamper_token(
            prepared.prepared_token,
            lambda value: value.update(
                prepared_at="2026-08-27T10:01:00+08:00",
                expires_at="2026-08-27T10:11:00+08:00",
            ),
        )
        with self.assertRaisesRegex(TokenError, "invalid"):
            stage_bind(
                prepared.prepared_token,
                derived_fields(),
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
            )


class CommitBindTests(BindTestBase):
    def test_commit_without_rename_confirmation_does_not_read_or_write(self):
        staged = self.stage()
        writes_before = len(self.store.writes)

        with self.assertRaises(RenameRequired) as caught:
            commit_bind(
                staged.staged_token,
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
                rename_confirmed=False,
            )

        self.assertEqual(caught.exception.error_code, "rename_required")
        self.assertEqual(len(self.store.writes), writes_before)
        self.assertTrue(Path(staged.staged_token).exists())

    def test_commit_creates_one_dossier_and_resume_command(self):
        staged = self.stage()
        completed = self.commit(staged)
        self.assertEqual(completed.resume_command, f"codex resume {SESSION_ID}")
        self.assertEqual(self.store.paths, {DOSSIER})
        self.assertEqual(completed.dossier_path, str(self.store.vault_path / DOSSIER))
        saved = Dossier.parse(self.store.read(DOSSIER))
        self.assertEqual(saved.records[REF].topics, ("idempotency", "payment callback"))
        self.assertTrue(saved.records[REF].result == "design selected")
        self.assertFalse(Path(staged.staged_token).exists())

    def test_same_session_concurrent_cross_item_commits_are_archive_atomic(self):
        vault = self.root / "filesystem-vault"
        vault.mkdir()
        store = StoreResolution(vault, "filesystem", FakeRunner())
        staged = []
        for work_item_id in ("REQ-123", "REQ-456"):
            prepared = prepare_bind(
                BindRequest(work_item_id, "concurrent bind", self.request.cwd),
                env=ENV,
                store=store,
                now=NOW,
                run=FakeRunner(),
                token_root=self.root / "process-tokens",
            )
            staged.append(
                stage_bind(
                    prepared.prepared_token,
                    derived_fields(),
                    store,
                    env=ENV,
                    now=NOW,
                    token_root=self.root / "process-tokens",
                )
            )

        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=commit_in_process,
                args=(
                    item.staged_token,
                    str(vault),
                    str(self.root / "process-tokens"),
                    str(self.root / "process-locks"),
                    work_item_id,
                    start,
                    results,
                ),
            )
            for item, work_item_id in zip(staged, ("REQ-123", "REQ-456"), strict=True)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            self.assertEqual(process.exitcode, 0)

        outcomes = sorted(results.get(timeout=1) for _ in processes)
        self.assertEqual(
            [outcome[0] for outcome in outcomes], ["failure", "success"]
        )
        failure = next(outcome for outcome in outcomes if outcome[0] == "failure")
        success = next(outcome for outcome in outcomes if outcome[0] == "success")
        self.assertEqual(failure[2], "binding_conflict")

        dossier_paths = tuple(
            (vault / "AI-Coding-Archive/WorkItems").glob("*.md")
        )
        self.assertEqual(len(dossier_paths), 1)
        saved = Dossier.parse(dossier_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(set(saved.records), {REF})
        recalled = operations.recall(success[1], store)
        self.assertEqual(recalled.work_item_id, success[1])
        self.assertEqual(
            [(session.agent_id, session.session_id) for session in recalled.sessions],
            [(REF.agent_id, REF.session_id)],
        )
        with self.assertRaises(operations.NotFound):
            operations.recall(failure[1], store)

    def test_commit_holds_archive_lock_through_snapshot_and_persistence(self):
        staged = self.stage()
        state = {"held": False}
        original_search = self.store.search
        original_write = self.store.write
        original_read = self.store.read

        def guarded_search(query: str):
            self.assertTrue(state["held"])
            return original_search(query)

        def guarded_write(relative_path: Path, text: str) -> None:
            self.assertTrue(state["held"])
            original_write(relative_path, text)

        def guarded_read(relative_path: Path) -> str:
            self.assertTrue(state["held"])
            return original_read(relative_path)

        self.store.search = guarded_search  # type: ignore[method-assign]
        self.store.write = guarded_write  # type: ignore[method-assign]
        self.store.read = guarded_read  # type: ignore[method-assign]

        @contextmanager
        def observed_lock(vault_path: Path, **kwargs):
            self.assertEqual(vault_path, self.store.vault_path)
            self.assertEqual(kwargs["lock_root"], self.root / "locks")
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        with mock.patch.object(
            operations, "archive_lock", side_effect=observed_lock
        ):
            result = commit_bind(
                staged.staged_token,
                self.store,
                env=ENV,
                now=NOW,
                token_root=self.root / "tokens",
                rename_confirmed=True,
                lock_root=self.root / "locks",
            )

        self.assertTrue(result.dossier_path.endswith("REQ-123.md"))
        self.assertFalse(state["held"])

    def test_busy_work_item_is_a_stable_partial_failure(self):
        staged = self.stage()

        with mock.patch.object(
            operations,
            "archive_lock",
            side_effect=WorkItemBusy("work item is busy"),
        ):
            with self.assertRaises(PartialBindFailure) as caught:
                self.commit(staged)

        self.assertEqual(caught.exception.cause_error_code, "work_item_busy")
        self.assertTrue(caught.exception.rename_already_completed)
        self.assertTrue(Path(staged.staged_token).exists())

    def test_commit_round_trips_empty_topics(self):
        completed = self.commit(self.stage(derived_fields(topics=[])))

        self.assertEqual(completed.warnings, ())
        saved = Dossier.parse(self.store.read(DOSSIER))
        self.assertEqual(saved.records[REF].topics, ())

    def test_commit_round_trips_topics_containing_projection_delimiter(self):
        topics = ["callback、idempotency", "无"]
        self.commit(self.stage(derived_fields(topics=topics)))

        saved = Dossier.parse(self.store.read(DOSSIER))
        self.assertEqual(saved.records[REF].topics, tuple(topics))

    def test_same_item_commit_refreshes_in_place(self):
        self.commit(self.stage(derived_fields(result="old")))
        later = "2026-08-27T10:20:00+08:00"
        self.commit(self.stage(derived_fields(result="new"), later), later)
        text = self.store.read(DOSSIER)
        self.assertEqual(text.count(f"session:{record_key(REF)}:start"), 1)
        self.assertEqual(Dossier.parse(text).records[REF].result, "new")

    def test_commit_rechecks_dossier_digest_and_global_binding(self):
        self.store.add(DOSSIER, dossier_with("REQ-123", "0191f8c0-7a11-7000-8000-000000000001"))
        staged = self.stage()
        writes_before = len(self.store.writes)
        self.store.add(
            DOSSIER,
            dossier_with("REQ-123", "0191f8c0-7a11-7000-8000-000000000002"),
        )
        with self.assertRaises(PartialBindFailure) as changed:
            self.commit(staged)
        self.assertIsInstance(changed.exception.__cause__, StaleDossier)
        self.assertEqual(changed.exception.cause_error_code, "stale_dossier")
        self.assertEqual(len(self.store.writes), writes_before)

        self.store.files.clear()
        staged = self.stage()
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER-9.md"),
            dossier_with("OTHER-9", SESSION_ID),
        )
        with self.assertRaises(PartialBindFailure) as conflict:
            self.commit(staged)
        self.assertIsInstance(conflict.exception.__cause__, BindingConflict)

    def test_refresh_commit_classifies_duplicate_current_ref_as_binding_conflict(self):
        self.store.add(DOSSIER, dossier_with("REQ-123", SESSION_ID))
        staged = self.stage()
        self.store.add(
            Path("AI-Coding-Archive/WorkItems/OTHER-9.md"),
            dossier_with("OTHER-9", SESSION_ID),
        )

        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)

        self.assertIsInstance(caught.exception.__cause__, BindingConflict)
        self.assertEqual(caught.exception.__cause__.error_code, "binding_conflict")

    def test_commit_failure_is_rename_only_partial_failure_and_keeps_token(self):
        staged = self.stage()

        def fail_write(relative_path: Path, text: str) -> None:
            raise OSError("disk full")

        self.store.write = fail_write  # type: ignore[method-assign]
        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)
        self.assertTrue(caught.exception.rename_already_completed)
        self.assertIn("rerun", str(caught.exception))
        self.assertTrue(Path(staged.staged_token).exists())

    def test_commit_archive_access_failure_is_partial(self):
        staged = self.stage()
        self.store.search = mock.Mock(side_effect=OSError("vault unavailable"))
        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)
        self.assertTrue(caught.exception.rename_already_completed)
        self.assertIn("rerun", str(caught.exception))

    def test_commit_archive_parse_failure_is_partial(self):
        staged = self.stage()
        self.store.add(DOSSIER, "type: ai-work-item-history\nmalformed")
        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)
        self.assertTrue(caught.exception.rename_already_completed)
        self.assertIn("rerun", str(caught.exception))

    def test_fresh_public_bind_recovers_after_write_succeeds_and_readback_fails(self):
        self.store = OneReadbackFailureStore(self.root / "vault")
        staged = self.stage(derived_fields(result="first write"))
        with self.assertRaises(PartialBindFailure):
            self.commit(staged)
        self.assertTrue(Path(staged.staged_token).exists())
        self.assertIn("first write", self.store.files[DOSSIER])

        fresh = self.stage(derived_fields(result="fresh recovery"))
        completed = self.commit(fresh)
        saved = Dossier.parse(self.store.files[DOSSIER])
        self.assertEqual(completed.resume_command, f"codex resume {SESSION_ID}")
        self.assertEqual(saved.records[REF].result, "fresh recovery")
        self.assertEqual(
            self.store.files[DOSSIER].count(f"session:{record_key(REF)}:start"), 1
        )

    def test_commit_readback_mismatch_is_partial_failure(self):
        staged = self.stage()
        original_write = self.store.write

        def corrupt_write(relative_path: Path, text: str) -> None:
            original_write(relative_path, text.replace("design selected", "corrupted"))

        self.store.write = corrupt_write  # type: ignore[method-assign]
        with self.assertRaises(PartialBindFailure):
            self.commit(staged)

    def test_commit_validates_rendered_dossier_before_store_write(self):
        staged = self.stage()

        with mock.patch.object(Dossier, "render", return_value="malformed"):
            with self.assertRaises(PartialBindFailure):
                self.commit(staged)

        self.assertEqual(self.store.writes, [])

    def test_committed_token_cleanup_failure_is_a_warning(self):
        staged = self.stage()

        with mock.patch.object(
            operations, "_remove_token", side_effect=OSError("cleanup failed")
        ):
            completed = self.commit(staged)

        self.assertEqual(len(completed.warnings), 1)
        self.assertIn("cleanup", completed.warnings[0])
        self.assertNotIn(staged.staged_token, completed.warnings[0])
        self.assertIn(REF, Dossier.parse(self.store.read(DOSSIER)).records)

    def test_commit_rejects_tampered_cross_field_token_before_write(self):
        staged = self.stage()
        path = Path(staged.staged_token)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["value"]["record"]["session_id"] = (
            "0191f8c0-7a11-7000-8000-000000000001"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)
        self.assertIsInstance(caught.exception.__cause__, TokenError)
        self.assertFalse(self.store.writes)

    def test_commit_rejects_title_that_differs_from_staged_session_title(self):
        staged = self.stage()

        def change_title(value):
            value["target_title"] = "REQ-123 payment-api alternate title"
            value["record"]["title"] = value["target_title"]

        tamper_token(staged.staged_token, change_title)
        with self.assertRaises(PartialBindFailure) as caught:
            self.commit(staged)
        self.assertIsInstance(caught.exception.__cause__, TokenError)
        self.assertFalse(self.store.writes)

    def test_commit_rejects_extended_future_and_invalid_derived_tokens(self):
        staged = self.stage()
        tamper_token(
            staged.staged_token,
            lambda value: value.update(expires_at="2026-08-27T10:11:00+08:00"),
        )
        with self.assertRaises(PartialBindFailure):
            self.commit(staged)

        staged = self.stage()
        tamper_token(
            staged.staged_token,
            lambda value: (
                value.update(
                    staged_at="2026-08-27T10:01:00+08:00",
                    expires_at="2026-08-27T10:11:00+08:00",
                ),
                value["record"].update(occurred_at="2026-08-27T10:01:00+08:00"),
            ),
        )
        with self.assertRaises(PartialBindFailure):
            self.commit(staged)

        mutations = (
            lambda value: value["record"].update(topics=["duplicate", "duplicate"]),
            lambda value: value["record"].update(result="bad\nresult"),
            lambda value: value["summary"].update(
                project_roles={value["project"]["project_name"]: " "}
            ),
            lambda value: value["summary"].update(
                evidence_sessions=[
                    {"agent_id": "codex", "session_id": SESSION_ID},
                    {"agent_id": "codex", "session_id": SESSION_ID},
                ]
            ),
            lambda value: value["summary"]["project_roles"].update(
                {"unrelated-project": "unrelated role"}
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                staged = self.stage()
                tamper_token(staged.staged_token, mutation)
                with self.assertRaises(PartialBindFailure):
                    self.commit(staged)

    def test_second_project_bind_preserves_first_project_role(self):
        self.commit(self.stage(derived_fields(project_role="callback owner")))
        later = "2026-08-27T10:05:00+08:00"
        second_env = {"CODEX_SESSION_ID": SECOND_SESSION_ID}
        prepared = prepare_bind(
            BindRequest("REQ-123", "订单设计", Path("/work/order-service/src")),
            env=second_env,
            store=self.store,
            now=later,
            run=SecondProjectRunner(),
            token_root=self.root / "tokens",
        )
        staged = stage_bind(
            prepared.prepared_token,
            derived_fields(project_role="order owner"),
            self.store,
            env=second_env,
            now=later,
            token_root=self.root / "tokens",
        )
        commit_bind(
            staged.staged_token,
            self.store,
            env=second_env,
            now=later,
            token_root=self.root / "tokens",
            rename_confirmed=True,
            lock_root=self.root / "locks",
        )
        roles = {
            key.casefold(): value
            for key, value in Dossier.parse(self.store.files[DOSSIER]).project_roles.items()
        }
        self.assertEqual(roles["payment-api"], "callback owner")
        self.assertEqual(roles["order-service"], "order owner")

    def test_commit_rechecks_expiry_runtime_session_and_vault(self):
        staged = self.stage()
        with self.assertRaises(PartialBindFailure):
            self.commit(staged, "2026-08-27T10:11:00+08:00")

        other_env = {"CODEX_SESSION_ID": "0191f8c0-7a11-7000-8000-000000000001"}
        with self.assertRaises(PartialBindFailure):
            commit_bind(
                staged.staged_token,
                self.store,
                env=other_env,
                now=NOW,
                token_root=self.root / "tokens",
                rename_confirmed=True,
                lock_root=self.root / "locks",
            )

        original_vault = self.store.vault_path
        self.store.vault_path = self.root / "different-vault"
        with self.assertRaises(PartialBindFailure):
            self.commit(staged)
        self.store.vault_path = original_vault


if __name__ == "__main__":
    unittest.main()
