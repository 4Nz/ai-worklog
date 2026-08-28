from __future__ import annotations

import sys
import unittest
import hashlib
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worklog.model import (  # noqa: E402
    SessionRef,
    ValidationError,
    record_key,
    validate_session_id,
    validate_session_title,
    validate_work_item_id,
)


class IdentityModelTests(unittest.TestCase):
    def test_record_key_uses_agent_nul_session_digest(self):
        ref = SessionRef("codex", "opaque/session")
        expected = hashlib.sha256(b"codex\0opaque/session").hexdigest()
        self.assertEqual(record_key(ref), expected)
        self.assertEqual(len(record_key(ref)), 64)

    def test_rejects_invalid_opaque_session_ids(self):
        for value in ("", "line\nbreak", "null\0byte", "x" * 257, "hidden\u200bcontrol"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_session_id(value)

    def test_identifier_and_chinese_title_contract(self):
        self.assertEqual(validate_work_item_id("REQ-123_a.1"), "REQ-123_a.1")
        self.assertEqual(validate_session_title(" 支付回调 幂等设计 "), "支付回调 幂等设计")
        for invalid in ("", "中文-1", "REQ 1", "../REQ", "REQ/1", "A" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_work_item_id(invalid)

    def test_rejects_control_characters_before_trimming_title(self):
        for invalid in ("\npayment", "payment\n", "\rpayment", "payment\u200b"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_session_title(invalid)
