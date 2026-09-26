import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TemplateRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _template(self, version, summary):
        return self.service.create(
            self.actor,
            "consent_template",
            {
                "template_code": "ICF-GENERAL",
                "version": version,
                "summary": summary,
                "purposes": ["research"],
                "validity_days": 365,
            },
        )

    def _consent(self, template):
        participant = self.service.create(
            self.actor, "participant", {"name": "Participant"}
        )
        return self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "template_id": template["id"]},
        )

    def test_revision_repoints_drafts_returns_signed_keeps_active_snapshot(self):
        v1 = self._template("v1", "第一版摘要")
        draft = self._consent(v1)
        signed = self._consent(v1)
        self.service.transition(self.actor, signed["id"], "sign", {"signed_at": "2026-01-10"})
        active = self._consent(v1)
        self.service.transition(self.actor, active["id"], "sign", {"signed_at": "2026-01-10"})
        self.service.transition(self.actor, active["id"], "activate", {})

        v2 = self._template("v2", "第二版摘要")

        self.assertEqual(self.service.get(v1["id"])["status"], "superseded")

        # 未签的草稿切到新版
        draft_after = self.service.get(draft["id"])
        self.assertEqual(draft_after["status"], "draft")
        self.assertEqual(draft_after["data"]["template_id"], v2["id"])

        # 签过但还没生效的退回补签，旧快照保留可追溯
        signed_after = self.service.get(signed["id"])
        self.assertEqual(signed_after["status"], "returned")
        self.assertEqual(signed_after["data"]["template_id"], v2["id"])
        self.assertIn("v1", signed_after["data"]["return_reason"])
        self.assertIn("v2", signed_after["data"]["return_reason"])
        self.assertEqual(signed_after["data"]["snapshot"]["template_version"], "v1")
        self.assertEqual(signed_after["data"]["snapshot"]["summary"], "第一版摘要")

        # 已经生效的仍按原快照办理
        active_after = self.service.get(active["id"])
        self.assertEqual(active_after["status"], "active")
        self.assertEqual(active_after["data"]["template_id"], v1["id"])
        self.assertEqual(active_after["data"]["snapshot"]["template_version"], "v1")
        self.assertEqual(active_after["data"]["snapshot"]["summary"], "第一版摘要")

        # 草稿按新版签署，冻结新版快照
        self.service.transition(self.actor, draft["id"], "sign", {"signed_at": "2026-02-01"})
        draft_signed = self.service.get(draft["id"])
        self.assertEqual(draft_signed["status"], "signed")
        self.assertEqual(draft_signed["data"]["snapshot"]["template_version"], "v2")
        self.assertEqual(draft_signed["data"]["snapshot"]["summary"], "第二版摘要")

        # 退回的补签后回到 signed，退回原因清除
        self.service.transition(self.actor, signed["id"], "sign", {"signed_at": "2026-02-01"})
        resigned = self.service.get(signed["id"])
        self.assertEqual(resigned["status"], "signed")
        self.assertEqual(resigned["data"]["snapshot"]["template_version"], "v2")
        self.assertIsNone(resigned["data"]["return_reason"])

    def test_loan_check_lists_snapshot_and_return_reason(self):
        v1 = self._template("v1", "第一版摘要")
        consent = self._consent(v1)
        self.service.transition(self.actor, consent["id"], "sign", {"signed_at": "2026-01-10"})
        self.service.transition(self.actor, consent["id"], "activate", {})
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": consent["data"]["participant_id"],
                "sample_code": "B-100",
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )

        # 模板改版不影响已生效同意书的快照，借出前核对仍看到签署时的内容
        self._template("v2", "第二版摘要")
        check = self.service.loan_check(sample["id"])
        self.assertTrue(check["loanable"])
        self.assertEqual(check["template_code"], "ICF-GENERAL")
        self.assertEqual(check["template_version"], "v1")
        self.assertEqual(check["summary"], "第一版摘要")
        self.assertEqual(check["purposes"], ["research"])
        self.assertEqual(check["expires_at"], "2027-01-10")
        self.assertIsNone(check["return_reason"])


if __name__ == "__main__":
    unittest.main()
