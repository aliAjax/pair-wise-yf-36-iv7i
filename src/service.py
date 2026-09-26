from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


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
        if kind == "consent_template":
            self._apply_template_revision(actor, entity)
        return entity

    def _apply_template_revision(self, actor, new_template):
        """模板改版：旧版作废，未签草稿切到新版，已签未生效的退回补签。"""
        code = new_template["data"].get("template_code")
        new_version = str(new_template["data"].get("version"))
        siblings = self.repository.find_entities("consent_template", "template_code", code)
        for old in siblings:
            if old["id"] == new_template["id"] or old["status"] != "published":
                continue
            old_version = str(old["data"].get("version"))
            self.repository.update_entity(old["id"], old["version"], "superseded", old["data"])
            self.audit.record(
                old["id"],
                actor,
                "supersede",
                "published",
                "superseded",
                {"reason": "replaced by version " + new_version, "new_template_id": new_template["id"]},
            )
            for consent in self.repository.find_entities("consent", "template_id", old["id"]):
                status = consent["status"]
                if status == "draft":
                    data = dict(consent["data"])
                    data["template_id"] = new_template["id"]
                    self.repository.update_entity(consent["id"], consent["version"], status, data)
                    self.audit.record(
                        consent["id"],
                        actor,
                        "repoint",
                        status,
                        status,
                        {"template_id": new_template["id"], "template_version": new_version},
                    )
                elif status in ("signed", "returned"):
                    data = dict(consent["data"])
                    data["template_id"] = new_template["id"]
                    data["return_reason"] = "模板改版 %s → %s，需按新版本重新签署" % (old_version, new_version)
                    self.repository.update_entity(consent["id"], consent["version"], "returned", data)
                    self.audit.record(
                        consent["id"],
                        actor,
                        "return",
                        status,
                        "returned",
                        {"return_reason": data["return_reason"], "template_id": new_template["id"]},
                    )
                # 已生效（active）及已归档的同意书仍按签署时冻结的快照办理

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
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def loan_check(self, sample_id):
        """借出前核对：返回样本关联同意书的签署快照、状态和退回原因。"""
        sample = self.repository.get_entity(sample_id)
        if not sample or sample["kind"] != "sample":
            raise NotFoundError("sample not found: " + sample_id)
        consent_id = sample["data"].get("consent_id")
        consent = self.repository.get_entity(consent_id) if consent_id else None
        snapshot = dict((consent or {}).get("data", {}).get("snapshot") or {})
        expires_at = snapshot.get("expires_at")
        today = datetime.now(timezone.utc).date().isoformat()
        checks = {
            "consent_active": bool(consent and consent["status"] == "active"),
            "snapshot_present": bool(snapshot),
            "not_expired": not expires_at or expires_at >= today,
        }
        return {
            "sample_id": sample["id"],
            "sample_status": sample["status"],
            "consent_id": consent_id,
            "consent_status": consent["status"] if consent else None,
            "template_code": snapshot.get("template_code"),
            "template_version": snapshot.get("template_version"),
            "summary": snapshot.get("summary"),
            "purposes": snapshot.get("purposes"),
            "expires_at": expires_at,
            "return_reason": consent["data"].get("return_reason") if consent else None,
            "checks": checks,
            "loanable": all(checks.values()) and sample["status"] == "stored",
        }

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
