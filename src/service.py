from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, check_loan_snapshot


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "template" and action == "publish":
            self._apply_template_revision(actor, updated)
        return updated

    def _apply_template_revision(self, actor, template):
        """模板改版：草稿切到新版，已签未生效的退回补签，已生效的保留原快照。"""
        old_id = template["data"].get("supersedes")
        if not old_id:
            return
        new_version = template["data"].get("version")
        old = self.repository.get_entity(old_id)
        if old and old["kind"] == "template" and old["status"] == "published":
            self.repository.update_entity(old["id"], old["version"], "archived", old["data"])
            self.audit.record(
                old["id"], actor, "archive", "published", "archived",
                {"reason": "superseded by template " + template["id"]},
            )
        for consent in self.repository.list_entities(kind="consent"):
            if consent["data"].get("template_id") != old_id:
                continue
            if consent["status"] == "draft":
                data = dict(consent["data"])
                data["template_id"] = template["id"]
                data["template_version"] = new_version
                self.repository.update_entity(
                    consent["id"], consent["version"], consent["status"], data
                )
                self.audit.record(
                    consent["id"], actor, "retemplate", consent["status"], consent["status"],
                    {"template_id": template["id"], "template_version": new_version},
                )
            elif consent["status"] == "signed":
                reason = "template revised to version %s; re-signature required" % new_version
                next_status, patch = self.rules.validate_transition(
                    actor, consent, "return_for_resign", {"reason": reason}, self._lookup
                )
                merged = dict(consent["data"])
                merged.update(patch)
                merged["template_id"] = template["id"]
                merged["template_version"] = new_version
                self.repository.update_entity(
                    consent["id"], consent["version"], next_status, merged
                )
                self.audit.record(
                    consent["id"], actor, "return_for_resign", consent["status"],
                    next_status, {"patch": patch},
                )
            # active consents keep their frozen snapshot untouched

    def loan_check(self, sample_id):
        sample = self.repository.get_entity(sample_id)
        if not sample or sample["kind"] != "sample":
            raise NotFoundError("sample not found: " + str(sample_id))
        consent_id = sample["data"].get("consent_id")
        consent = self.repository.get_entity(consent_id) if consent_id else None
        snapshot, reasons = check_loan_snapshot(consent)
        if sample["status"] != "stored":
            reasons.append("sample is not stored (status: %s)" % sample["status"])
        return {
            "sample_id": sample["id"],
            "sample_status": sample["status"],
            "consent_id": consent_id,
            "template_version": snapshot.get("template_version"),
            "summary": snapshot.get("summary"),
            "scope": snapshot.get("scope"),
            "expires_at": snapshot.get("expires_at"),
            "ok": not reasons,
            "reasons": reasons,
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
