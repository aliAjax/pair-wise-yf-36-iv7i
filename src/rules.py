from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_participant(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("participant name is required")


def _validate_template(actor, data, lookup):
    if not data.get("summary"):
        raise ValidationError("template summary is required")


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    template = _find_one(lookup, "template", "id", data.get("template_id"))
    if not template or template["status"] != "published":
        raise ValidationError("consent requires a published template")
    data["template_version"] = template["data"].get("version")


def _validate_consent_sign(actor, entity, data, lookup):
    template = _find_one(lookup, "template", "id", entity["data"].get("template_id"))
    if not template or template["status"] != "published":
        raise ValidationError("consent template is not published")
    try:
        _date_ordinal(data.get("expires_at"))
    except (TypeError, ValueError):
        raise ValidationError("expires_at must be an ISO date")
    snapshot = {
        "template_id": template["id"],
        "template_version": template["data"].get("version"),
        "summary": template["data"].get("summary"),
        "scope": list(data.get("scope") or []),
        "expires_at": data.get("expires_at"),
        "signed_at": data.get("signed_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return {"snapshot": snapshot, "template_version": snapshot["template_version"]}


def check_loan_snapshot(consent, due_at=None):
    """核对借出前的同意快照，返回 (snapshot, reasons)。"""
    reasons = []
    snapshot = {}
    if not consent:
        reasons.append("linked consent not found")
    elif consent["status"] != "active":
        reasons.append("consent is not active (status: %s)" % consent["status"])
    else:
        snapshot = consent["data"].get("snapshot") or {}
        if not snapshot:
            reasons.append("consent has no frozen snapshot")
        else:
            if "research" not in (snapshot.get("scope") or []):
                reasons.append("snapshot scope does not cover research use")
            expires = snapshot.get("expires_at")
            try:
                expiry = _date_ordinal(expires)
            except (TypeError, ValueError):
                reasons.append("snapshot expires_at is invalid")
            else:
                today = datetime.now(timezone.utc).date().toordinal()
                if expiry < today:
                    reasons.append("snapshot expired at %s" % expires)
                if due_at:
                    try:
                        if expiry < _date_ordinal(due_at):
                            reasons.append("snapshot expires before loan due date")
                    except (TypeError, ValueError):
                        reasons.append("due_at must be an ISO date")
    return snapshot, reasons


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    snapshot = consent["data"].get("snapshot") or {}
    scope = snapshot.get("scope") or consent["data"].get("scope") or []
    if "research" not in scope:
        raise ValidationError("consent does not include research use")
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _validate_sample_loan(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", entity["data"].get("consent_id"))
    snapshot, reasons = check_loan_snapshot(consent, data.get("due_at"))
    if reasons:
        raise ValidationError("loan blocked: " + "; ".join(reasons))
    return {
        "loan_snapshot": {
            "consent_id": consent["id"],
            "template_id": snapshot.get("template_id"),
            "template_version": snapshot.get("template_version"),
            "summary": snapshot.get("summary"),
            "scope": snapshot.get("scope"),
            "expires_at": snapshot.get("expires_at"),
        }
    }


def _validate_template_publish(actor, entity, data, lookup):
    supersedes = data.get("supersedes") or entity["data"].get("supersedes")
    if supersedes:
        old = _find_one(lookup, "template", "id", supersedes)
        if not old:
            raise ValidationError("unknown superseded template: " + str(supersedes))
    return {"supersedes": supersedes} if supersedes else {}


def _validate_withdrawal_approve(actor, entity, data, lookup):
    samples = data.get("sample_ids") or []
    if len(set(samples)) != len(samples):
        raise ConflictError("sample_ids contains duplicates")
    for sample_id in samples:
        if not _find_one(lookup, "sample", "id", sample_id):
            raise ValidationError("unknown sample: " + str(sample_id))
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'participant': _validate_participant, 'consent': _validate_consent, 'template': _validate_template}
CUSTOM_TRANSITIONS = {('consent', 'sign'): _validate_consent_sign, ('sample', 'store'): _validate_sample_store, ('sample', 'loan'): _validate_sample_loan, ('template', 'publish'): _validate_template_publish, ('withdrawal', 'approve'): _validate_withdrawal_approve}


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal', 'templates': 'template'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested', 'template': 'draft'}
    TRANSITIONS = {'participant': {'close_participant': (('registered',), 'closed')}, 'consent': {'sign': (('draft', 'returned'), 'signed'), 'activate': (('signed',), 'active'), 'return_for_resign': (('signed',), 'returned'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')}, 'sample': {'store': (('collected',), 'stored'), 'loan': (('stored',), 'on_loan'), 'return': (('on_loan',), 'stored'), 'anonymize': (('stored',), 'anonymized'), 'destroy': (('stored',), 'destroyed')}, 'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')}, 'template': {'publish': (('draft',), 'published'), 'archive': (('published',), 'archived')}}
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'template_id'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at'), 'template': ('title', 'version', 'summary')}
    ACTION_REQUIRED = {('consent', 'sign'): ('scope', 'expires_at'), ('consent', 'return_for_resign'): ('reason',), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank'), 'template': ('admin', 'committee')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'sign': ('admin', 'committee'), 'activate': ('admin', 'committee'), 'return_for_resign': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'execute': ('admin', 'biobank'), 'publish': ('admin', 'committee'), 'archive': ('admin', 'committee')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
