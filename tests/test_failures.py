import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'participant', {'name': 'Participant'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'close_participant',
                {},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'participant', {'name': 'Participant'}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"),
                entity["id"],
                'close_participant',
                {},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'participant',
            {'name': 'Participant'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'participant',
            {'name': 'Participant'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def _stored_sample(self, purposes=('research',), validity_days=365, signed_at='2026-01-01'):
        admin = Actor("admin", "admin")
        participant = self.service.create(admin, 'participant', {'name': 'Participant'})
        template = self.service.create(
            admin,
            'consent_template',
            {
                'template_code': 'ICF-GENERAL',
                'version': 'v1',
                'summary': '同意样本用于未来医学研究',
                'purposes': list(purposes),
                'validity_days': validity_days,
            },
        )
        consent = self.service.create(
            admin,
            'consent',
            {'participant_id': participant["id"], 'template_id': template["id"]},
        )
        self.service.transition(admin, consent["id"], 'sign', {'signed_at': signed_at})
        self.service.transition(admin, consent["id"], 'activate', {})
        sample = self.service.create(
            admin,
            'sample',
            {'participant_id': participant["id"], 'sample_code': 'B-1', 'collected_at': '2026-01-01'},
        )
        self.service.transition(
            admin,
            sample["id"],
            'store',
            {'freezer': 'F1', 'position': 'A1', 'consent_id': consent["id"]},
        )
        return sample

    def test_consent_requires_published_template(self):
        admin = Actor("admin", "admin")
        participant = self.service.create(admin, 'participant', {'name': 'Participant'})
        with self.assertRaises(ValidationError):
            self.service.create(
                admin,
                'consent',
                {'participant_id': participant["id"], 'template_id': 'missing-template'},
            )

    def test_duplicate_template_version_rejected(self):
        admin = Actor("admin", "admin")
        data = {
            'template_code': 'ICF-GENERAL',
            'version': 'v1',
            'summary': '摘要',
            'purposes': ['research'],
            'validity_days': 365,
        }
        self.service.create(admin, 'consent_template', data)
        with self.assertRaises(ConflictError):
            self.service.create(admin, 'consent_template', data)

    def test_unsigned_consent_cannot_activate(self):
        admin = Actor("admin", "admin")
        participant = self.service.create(admin, 'participant', {'name': 'Participant'})
        template = self.service.create(
            admin,
            'consent_template',
            {
                'template_code': 'ICF-GENERAL',
                'version': 'v1',
                'summary': '摘要',
                'purposes': ['research'],
                'validity_days': 365,
            },
        )
        consent = self.service.create(
            admin,
            'consent',
            {'participant_id': participant["id"], 'template_id': template["id"]},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(admin, consent["id"], 'activate', {})

    def test_loan_purpose_outside_snapshot_rejected(self):
        sample = self._stored_sample()
        with self.assertRaises(ValidationError):
            self.service.transition(
                Actor("admin", "admin"),
                sample["id"],
                'loan',
                {'recipient': 'Lab A', 'purpose': 'commercial', 'due_at': '2026-12-01', 'loaned_at': '2026-02-01'},
            )

    def test_loan_expired_snapshot_rejected(self):
        sample = self._stored_sample(validity_days=30)
        with self.assertRaises(ValidationError):
            self.service.transition(
                Actor("admin", "admin"),
                sample["id"],
                'loan',
                {'recipient': 'Lab A', 'purpose': 'research', 'due_at': '2026-12-01', 'loaned_at': '2026-06-01'},
            )


if __name__ == "__main__":
    unittest.main()
