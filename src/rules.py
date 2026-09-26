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


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    template = _find_one(lookup, "consent_template", "id", data.get("template_id"))
    if not template or template["status"] != "published":
        raise ValidationError("consent requires a published consent template")


def _validate_consent_template(actor, data, lookup):
    purposes = data.get("purposes")
    if not isinstance(purposes, list) or not all(str(item).strip() for item in purposes):
        raise ValidationError("template purposes must be a non-empty list")
    try:
        validity = int(data.get("validity_days"))
    except (TypeError, ValueError):
        validity = 0
    if validity <= 0:
        raise ValidationError("template validity_days must be a positive integer")
    if not str(data.get("summary") or "").strip():
        raise ValidationError("template summary is required")
    siblings = lookup("consent_template", "template_code", data.get("template_code")) if lookup else []
    for template in siblings or []:
        if str(template["data"].get("version")) == str(data.get("version")):
            raise ConflictError(
                "template version already exists: %s %s"
                % (data.get("template_code"), data.get("version"))
            )


def _validate_consent_sign(actor, entity, data, lookup):
    template = _find_one(lookup, "consent_template", "id", entity["data"].get("template_id"))
    if not template or template["status"] != "published":
        raise ValidationError("cannot sign against an unpublished template")
    template_data = template["data"]
    signed_at = str(data.get("signed_at"))[:10]
    try:
        signed_date = datetime.fromisoformat(signed_at).date()
    except ValueError:
        raise ValidationError("signed_at must be an ISO date")
    expires_at = data.get("expires_at")
    if expires_at:
        expires_at = str(expires_at)[:10]
    else:
        expires_at = (
            signed_date + timedelta(days=int(template_data.get("validity_days", 0)))
        ).isoformat()
    purposes = data.get("purposes") or template_data.get("purposes") or []
    snapshot = {
        "template_id": template["id"],
        "template_code": template_data.get("template_code"),
        "template_version": template_data.get("version"),
        "summary": template_data.get("summary"),
        "purposes": list(purposes),
        "signed_at": signed_at,
        "signed_by": actor.user_id,
        "expires_at": expires_at,
    }
    return {"snapshot": snapshot, "return_reason": None}


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    snapshot = consent["data"].get("snapshot") or {}
    if "research" not in (snapshot.get("purposes") or []):
        raise ValidationError("consent snapshot does not include research use")
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _validate_sample_loan(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", entity["data"].get("consent_id"))
    if not consent:
        raise ValidationError("sample is not linked to any consent")
    if consent["status"] != "active":
        message = "loan requires an active consent, found " + consent["status"]
        reason = consent["data"].get("return_reason")
        if reason:
            message += " (" + str(reason) + ")"
        raise ValidationError(message)
    snapshot = consent["data"].get("snapshot") or {}
    if data.get("purpose") not in (snapshot.get("purposes") or []):
        raise ValidationError("loan purpose is not covered by the signed consent snapshot")
    loaned_at = str(data.get("loaned_at") or _today())[:10]
    try:
        loaned_ordinal = _date_ordinal(loaned_at)
    except ValueError:
        raise ValidationError("loaned_at must be an ISO date")
    expires_at = snapshot.get("expires_at")
    if expires_at and loaned_ordinal > _date_ordinal(expires_at):
        raise ValidationError("consent snapshot expired at " + str(expires_at))
    return {"loaned_at": loaned_at}


def _validate_withdrawal_approve(actor, entity, data, lookup):
    samples = data.get("sample_ids") or []
    if len(set(samples)) != len(samples):
        raise ConflictError("sample_ids contains duplicates")
    for sample_id in samples:
        if not _find_one(lookup, "sample", "id", sample_id):
            raise ValidationError("unknown sample: " + str(sample_id))
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'participant': _validate_participant, 'consent': _validate_consent, 'consent_template': _validate_consent_template}
CUSTOM_TRANSITIONS = {('consent', 'sign'): _validate_consent_sign, ('sample', 'store'): _validate_sample_store, ('sample', 'loan'): _validate_sample_loan, ('withdrawal', 'approve'): _validate_withdrawal_approve}


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'consent_templates': 'consent_template', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'consent_template': 'published', 'sample': 'collected', 'withdrawal': 'requested'}
    TRANSITIONS = {'participant': {'close_participant': (('registered',), 'closed')}, 'consent': {'sign': (('draft', 'returned'), 'signed'), 'activate': (('signed',), 'active'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')}, 'consent_template': {}, 'sample': {'store': (('collected',), 'stored'), 'loan': (('stored',), 'on_loan'), 'return': (('on_loan',), 'stored'), 'anonymize': (('stored',), 'anonymized'), 'destroy': (('stored',), 'destroyed')}, 'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')}}
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'template_id'), 'consent_template': ('template_code', 'version', 'summary', 'purposes', 'validity_days'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at')}
    ACTION_REQUIRED = {('consent', 'sign'): ('signed_at',), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'consent_template': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'sign': ('admin', 'committee'), 'activate': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'execute': ('admin', 'biobank')}

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


def _today():
    return datetime.now(timezone.utc).date().isoformat()
