import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TemplateRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("committee-1", "committee")
        self.template_v1 = self.service.create(
            self.actor,
            "template",
            {"title": "通用知情同意书", "version": "v1", "summary": "旧版摘要"},
        )
        self.service.transition(self.actor, self.template_v1["id"], "publish", {})
        self.participant = self.service.create(
            Actor("admin", "admin"), "participant", {"name": "Participant One"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _make_consent(self, sign=False, activate=False, expires="2099-01-01"):
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": self.participant["id"], "template_id": self.template_v1["id"]},
        )
        if sign or activate:
            self.service.transition(
                self.actor,
                consent["id"],
                "sign",
                {"scope": ["research"], "expires_at": expires},
            )
        if activate:
            self.service.transition(self.actor, consent["id"], "activate", {})
        return self.service.get(consent["id"])

    def _publish_v2(self):
        template_v2 = self.service.create(
            self.actor,
            "template",
            {
                "title": "通用知情同意书",
                "version": "v2",
                "summary": "伦理委员会修订后的摘要",
                "supersedes": self.template_v1["id"],
            },
        )
        return self.service.transition(self.actor, template_v2["id"], "publish", {})

    def test_sign_freezes_snapshot(self):
        consent = self._make_consent(sign=True)
        snapshot = consent["data"]["snapshot"]
        self.assertEqual(snapshot["template_id"], self.template_v1["id"])
        self.assertEqual(snapshot["template_version"], "v1")
        self.assertEqual(snapshot["summary"], "旧版摘要")
        self.assertEqual(snapshot["scope"], ["research"])
        self.assertEqual(snapshot["expires_at"], "2099-01-01")
        self.assertTrue(snapshot["signed_at"])

    def test_draft_switches_to_new_template(self):
        draft = self._make_consent()
        template_v2 = self._publish_v2()
        updated = self.service.get(draft["id"])
        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["data"]["template_id"], template_v2["id"])
        self.assertEqual(updated["data"]["template_version"], "v2")

    def test_signed_consent_returned_for_resign(self):
        signed = self._make_consent(sign=True)
        template_v2 = self._publish_v2()
        updated = self.service.get(signed["id"])
        self.assertEqual(updated["status"], "returned")
        self.assertIn("v2", updated["data"]["reason"])
        self.assertEqual(updated["data"]["template_id"], template_v2["id"])
        # 补签后冻结新版快照并可生效
        self.service.transition(
            self.actor,
            signed["id"],
            "sign",
            {"scope": ["research"], "expires_at": "2099-06-01"},
        )
        resigned = self.service.get(signed["id"])
        self.assertEqual(resigned["data"]["snapshot"]["template_version"], "v2")
        self.assertEqual(resigned["data"]["snapshot"]["summary"], "伦理委员会修订后的摘要")
        self.service.transition(self.actor, signed["id"], "activate", {})
        self.assertEqual(self.service.get(signed["id"])["status"], "active")

    def test_active_consent_keeps_original_snapshot(self):
        active = self._make_consent(activate=True)
        self._publish_v2()
        updated = self.service.get(active["id"])
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["data"]["snapshot"]["template_version"], "v1")
        self.assertEqual(updated["data"]["snapshot"]["summary"], "旧版摘要")

    def test_old_template_archived_after_revision(self):
        self._publish_v2()
        self.assertEqual(self.service.get(self.template_v1["id"])["status"], "archived")

    def test_loan_blocked_after_consent_returned(self):
        consent = self._make_consent(activate=True)
        sample = self.service.create(
            Actor("admin", "admin"),
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": "B-100",
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            Actor("admin", "admin"),
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )
        # 模板改版导致另一份已签同意书被退回；本同意书已生效不受影响
        self._publish_v2()
        check = self.service.loan_check(sample["id"])
        self.assertTrue(check["ok"])
        self.assertEqual(check["template_version"], "v1")
        # 手工退回（如委员会要求补签）后借出被拦截
        self.service.transition(
            self.actor, consent["id"], "supersede", {"reason": "template revised"}
        )
        check = self.service.loan_check(sample["id"])
        self.assertFalse(check["ok"])
        self.assertTrue(check["reasons"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                Actor("admin", "admin"),
                sample["id"],
                "loan",
                {"recipient": "Lab A", "purpose": "assay", "due_at": "2027-01-01"},
            )

    def test_loan_blocked_when_snapshot_expired(self):
        consent = self._make_consent(activate=True, expires="2020-01-01")
        sample = self.service.create(
            Actor("admin", "admin"),
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": "B-200",
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            Actor("admin", "admin"),
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A2", "consent_id": consent["id"]},
        )
        check = self.service.loan_check(sample["id"])
        self.assertFalse(check["ok"])
        self.assertTrue(any("expired" in reason for reason in check["reasons"]))


if __name__ == "__main__":
    unittest.main()
