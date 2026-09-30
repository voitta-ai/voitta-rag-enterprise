"""LLM credentials: storage, resolution and verification.

Storage
    ``assistant_credentials`` rows, secret encrypted with services/secret_box.
    Two scopes: ``deployment`` (one per kind, managed by assistant admins)
    and ``person`` (keyed by lowercased email — a person, not one of their
    account rows; only ``anthropic_api_key``). The Claude subscription
    token exists only at deployment scope: every super-admin shares it.
    ``VOITTA_ASSISTANT_API_KEY`` supplies a read-only deployment API key
    for environments whose secrets are managed outside the app.

Resolution (who pays) — always the REAL person, never an impersonated one:
    ``anthropic_api``        person key → stored deployment key → env key
    ``claude_subscription``  deployment token, and only for assistant admins

Plaintext secrets exist only in :class:`ResolvedCredential` values handed
to an engine for one turn; nothing returned to a client ever carries more
than ``hint`` (a masked tail).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import AssistantCredential
from .. import secret_box
from .catalog import ENGINE_CREDENTIAL, CredentialKind, EngineId
from .identity import AssistantIdentity

logger = logging.getLogger(__name__)

Scope = Literal["deployment", "person"]
ProbeResult = Literal["ok", "auth_failed", "inconclusive"]


class CredentialError(Exception):
    """Invalid credential input."""


class CredentialForbidden(CredentialError):
    """The caller may not manage this credential."""


@dataclass(frozen=True)
class ResolvedCredential:
    kind: CredentialKind
    secret: str
    # Where it came from: 'person' | 'deployment' | 'environment'.
    source: str
    # Row id for recording verification results; None for the env key.
    credential_id: int | None


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    hint: str = ""
    source: str = ""
    last_verified_at: int | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "hint": self.hint,
            "source": self.source,
            "last_verified_at": self.last_verified_at,
            "last_error": self.last_error,
        }


def _scope_key(scope: Scope, email: str | None) -> str:
    if scope == "deployment":
        return ""
    if not email:
        raise CredentialError("a personal credential needs an email")
    return email.strip().lower()


def _row(db: Session, scope: Scope, key: str, kind: CredentialKind) -> AssistantCredential | None:
    return db.execute(
        select(AssistantCredential).where(
            AssistantCredential.scope == scope,
            AssistantCredential.scope_key == key,
            AssistantCredential.kind == kind,
        )
    ).scalar_one_or_none()


def _validate_secret(kind: CredentialKind, secret: str) -> str:
    value = secret.strip()
    if not value or any(c.isspace() for c in value):
        raise CredentialError("the credential is empty or contains whitespace")
    if kind == "claude_oauth_token" and not value.startswith("sk-ant-oat"):
        raise CredentialError(
            "that is not a Claude subscription token — run `claude setup-token` "
            "and paste the sk-ant-oat… value it prints"
        )
    if kind == "anthropic_api_key" and not value.startswith("sk-ant-"):
        raise CredentialError("that is not an Anthropic API key (sk-ant-…)")
    return value


def _check_may_write(ident: AssistantIdentity, scope: Scope, kind: CredentialKind) -> None:
    if scope == "deployment" and not ident.is_admin:
        raise CredentialForbidden("only super-admins manage deployment credentials")
    if scope == "person" and kind != "anthropic_api_key":
        raise CredentialForbidden("the Claude subscription is shared by the deployment")


def store(
    db: Session,
    ident: AssistantIdentity,
    scope: Scope,
    kind: CredentialKind,
    secret: str,
) -> CredentialStatus:
    """Create or replace a credential. Personal credentials belong to the
    real person, never to an impersonated account."""
    _check_may_write(ident, scope, kind)
    value = _validate_secret(kind, secret)
    key = _scope_key(scope, ident.real.email)
    now = int(time.time())
    row = _row(db, scope, key, kind)
    if row is None:
        row = AssistantCredential(scope=scope, scope_key=key, kind=kind, secret_enc="")
        db.add(row)
    row.secret_enc = secret_box.encrypt(value)
    row.hint = secret_box.mask(value)
    row.created_by = ident.real.email
    row.updated_at = now
    row.last_verified_at = None
    row.last_error = None
    db.flush()
    return _status_of(row, scope)


def delete(db: Session, ident: AssistantIdentity, scope: Scope, kind: CredentialKind) -> None:
    _check_may_write(ident, scope, kind)
    row = _row(db, scope, _scope_key(scope, ident.real.email), kind)
    if row is not None:
        db.delete(row)
        db.flush()


def _status_of(row: AssistantCredential, source: str) -> CredentialStatus:
    return CredentialStatus(
        configured=True,
        hint=row.hint,
        source=source,
        last_verified_at=row.last_verified_at,
        last_error=row.last_error,
    )


def _decrypted(row: AssistantCredential) -> str | None:
    try:
        return secret_box.decrypt(row.secret_enc)
    except secret_box.SecretUnavailable:
        logger.warning(
            "assistant credential %s/%s cannot be decrypted (secret key changed?) — "
            "treating it as not configured",
            row.scope,
            row.kind,
        )
        return None


def _deployment_api_key(db: Session) -> ResolvedCredential | None:
    row = _row(db, "deployment", "", "anthropic_api_key")
    if row is not None and (secret := _decrypted(row)):
        return ResolvedCredential("anthropic_api_key", secret, "deployment", row.id)
    env_key = get_settings().assistant_api_key
    if env_key:
        return ResolvedCredential("anthropic_api_key", env_key.strip(), "environment", None)
    return None


def resolve(db: Session, ident: AssistantIdentity, engine: EngineId) -> ResolvedCredential | None:
    """The credential a turn on ``engine`` runs on, or ``None``."""
    kind = ENGINE_CREDENTIAL[engine]
    if kind == "claude_oauth_token":
        if not ident.can_use_subscription:
            return None
        row = _row(db, "deployment", "", kind)
        if row is not None and (secret := _decrypted(row)):
            return ResolvedCredential(kind, secret, "deployment", row.id)
        return None
    row = _row(db, "person", _scope_key("person", ident.real.email), kind)
    if row is not None and (secret := _decrypted(row)):
        return ResolvedCredential(kind, secret, "person", row.id)
    return _deployment_api_key(db)


def status(db: Session, ident: AssistantIdentity) -> dict[str, dict[str, object]]:
    """What the settings screen shows. Deployment hints and errors only to
    assistant admins; everyone learns whether a deployment key exists."""
    out: dict[str, dict[str, object]] = {}
    person = _row(db, "person", _scope_key("person", ident.real.email), "anthropic_api_key")
    out["person_api_key"] = (
        _status_of(person, "person") if person is not None else CredentialStatus(False)
    ).as_dict()

    dep = _row(db, "deployment", "", "anthropic_api_key")
    if dep is not None:
        dep_status = _status_of(dep, "deployment")
    elif get_settings().assistant_api_key:
        dep_status = CredentialStatus(True, hint="(environment)", source="environment")
    else:
        dep_status = CredentialStatus(False)
    if not ident.is_admin:
        dep_status = CredentialStatus(dep_status.configured, source=dep_status.source)
    out["deployment_api_key"] = dep_status.as_dict()

    if ident.is_admin:
        sub = _row(db, "deployment", "", "claude_oauth_token")
        out["subscription"] = (
            _status_of(sub, "deployment") if sub is not None else CredentialStatus(False)
        ).as_dict()
    return out


def record_verification(db: Session, credential_id: int | None, error: str | None) -> None:
    """Stamp a probe or real-turn outcome on the stored row."""
    if credential_id is None:
        return
    row = db.get(AssistantCredential, credential_id)
    if row is None:
        return
    row.last_verified_at = int(time.time())
    row.last_error = error
    db.flush()


def for_probe(
    db: Session, ident: AssistantIdentity, scope: Scope, kind: CredentialKind
) -> ResolvedCredential | None:
    """The stored credential a Test button refers to (write rules apply)."""
    _check_may_write(ident, scope, kind)
    if kind == "anthropic_api_key" and scope == "deployment":
        return _deployment_api_key(db)
    row = _row(db, scope, _scope_key(scope, ident.real.email), kind)
    if row is None or not (secret := _decrypted(row)):
        return None
    return ResolvedCredential(kind, secret, scope, row.id)


async def probe(cred: ResolvedCredential) -> tuple[ProbeResult, str | None]:
    """Ask the provider whether ``cred`` authenticates.

    ``inconclusive`` (network down, CLI missing, timeout) says nothing about
    the credential — callers must not treat it as a failure.
    """
    if cred.kind == "anthropic_api_key":
        from .engines.anthropic_api import probe_api_key

        return await probe_api_key(cred.secret)
    from .engines.claude_subscription import probe_oauth_token

    return await probe_oauth_token(cred.secret)


def deployment_configured(db: Session) -> dict[str, bool]:
    """Which deployment-scope credentials exist (no secrets, no hints)."""
    return {
        "deployment_api_key_configured": _deployment_api_key(db) is not None,
        "subscription_configured": _row(db, "deployment", "", "claude_oauth_token") is not None,
    }
