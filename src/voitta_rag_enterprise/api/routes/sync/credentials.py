"""Company-scoped reusable sync credentials.

"Configure the Google client once per org": a credential row holds either
an OAuth client (id+secret, plus the refresh token once consent has been
granted — stored on the credential so ONE consent serves every folder
referencing it) or a service-account JSON. Folder sync sources reference
one via ``gd_credential_id`` instead of carrying inline copies.

Routes live on ``oauth_router`` (``/sync/...``, no folder_id): credentials
are company-level, not folder-level. Access rule is the company boundary
only — any member can list/create/delete (mirrors company_api_keys;
external gateways like Agnitio enforce their own admin gate before
proxying). Secrets never round-trip: list responses mask them to has_*
booleans, and the consent flow reuses the same Google callback URL as
folder-level OAuth, so no new GCP registration is needed.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource, SyncCredential
from ....services.acl import CurrentUser
from ....services.sync.google_drive import get_auth_url as gd_get_auth_url
from ...deps import current_user, db_session
from .base import external_redirect_uri, oauth_router

# OAuth ``state`` prefix distinguishing a credential-level consent from the
# folder-level flow (whose state is a bare int). See google_drive.py's
# callback, which branches on this.
CRED_STATE_PREFIX = "cred:"

KINDS = ("google_oauth_client", "google_service_account")


class SyncCredentialOut(BaseModel):
    id: int
    kind: str
    label: str
    client_id: str
    has_client_secret: bool
    has_service_account: bool
    connected: bool  # oauth kind: a refresh_token has been stored
    connected_email: str
    created_by: str
    created_at: int
    # Folders currently referencing this credential — drives the UI's
    # "in use by N vaults" hint and explains why delete may be refused.
    in_use_by: int


class SyncCredentialIn(BaseModel):
    kind: Literal["google_oauth_client", "google_service_account"]
    label: str = ""
    client_id: str = ""
    client_secret: str = ""
    service_account_json: str = ""


class CredAuthInitOut(BaseModel):
    auth_url: str


def _ref_count(db: Session, cred_id: int) -> int:
    return len(
        db.execute(
            select(FolderSyncSource.folder_id).where(
                FolderSyncSource.gd_credential_id == cred_id
            )
        ).all()
    )


def _to_out(db: Session, cred: SyncCredential) -> SyncCredentialOut:
    return SyncCredentialOut(
        id=cred.id,
        kind=cred.kind,
        label=cred.label or "",
        client_id=cred.client_id or "",
        has_client_secret=bool(cred.client_secret),
        has_service_account=bool(cred.service_account_json),
        connected=bool(cred.refresh_token),
        connected_email=cred.connected_email or "",
        created_by=cred.created_by or "",
        created_at=cred.created_at,
        in_use_by=_ref_count(db, cred.id),
    )


def get_company_credential(
    db: Session, user: CurrentUser, cred_id: int
) -> SyncCredential:
    """Load a credential enforcing the company boundary (404 outside it)."""
    cred = db.get(SyncCredential, cred_id)
    if cred is None or cred.company_id != user.company_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
    return cred


@oauth_router.get("/credentials", response_model=list[SyncCredentialOut])
def list_credentials(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[SyncCredentialOut]:
    rows = (
        db.execute(
            select(SyncCredential)
            .where(SyncCredential.company_id == user.company_id)
            .order_by(SyncCredential.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_to_out(db, c) for c in rows]


@oauth_router.post("/credentials", response_model=SyncCredentialOut)
def create_credential(
    body: SyncCredentialIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncCredentialOut:
    if body.kind == "google_oauth_client":
        if not (body.client_id.strip() and body.client_secret.strip()):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "An OAuth client credential needs client_id and client_secret",
            )
    else:
        sa = body.service_account_json.strip()
        if not sa:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "A service-account credential needs the key JSON",
            )
        try:
            parsed = json.loads(sa)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "The service-account key isn't valid JSON",
            ) from e
        if parsed.get("type") != "service_account":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                'The JSON doesn\'t look like a service-account key (missing '
                '"type": "service_account")',
            )

    now = int(time.time())
    cred = SyncCredential(
        company_id=user.company_id,
        kind=body.kind,
        label=body.label.strip(),
        client_id=body.client_id.strip(),
        client_secret=body.client_secret.strip(),
        service_account_json=body.service_account_json.strip(),
        created_by=user.email,
        created_at=now,
        updated_at=now,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return _to_out(db, cred)


@oauth_router.delete("/credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    cred_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    cred = get_company_credential(db, user, cred_id)
    refs = _ref_count(db, cred.id)
    if refs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This credential is used by {refs} synced folder(s). "
            "Point them at another credential first.",
        )
    db.delete(cred)
    db.commit()


@oauth_router.post(
    "/credentials/import-from-folder/{folder_id}",
    response_model=SyncCredentialOut,
)
def import_credential_from_folder(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncCredentialOut:
    """Promote a folder's INLINE Google credential into the shared registry.

    Pre-registry folders carry their client/secret/refresh-token inline on
    the sync-source row; this copies them server-side (secrets never cross
    the wire) — including the refresh token, so an existing consent carries
    over and no new popup is needed.

    Access is read-level (the folder must be visible to the caller): the
    copy only touches company-shared state, and this is how a credential
    configured by a colleague becomes reusable org-wide. Re-pointing the
    source folder at the new credential additionally requires ownership —
    non-owners get the copy while the folder keeps its inline fields.
    """
    from ....db.models import Folder
    from ....services.acl import is_folder_owner, user_can_see_folder

    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The folder has no Google Drive sync source"
        )
    if src.gd_credential_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This folder already uses a shared credential",
        )
    if getattr(src, "gd_use_builtin", False):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The built-in desktop client cannot be promoted to a shared credential",
        )
    has_oauth = bool(src.gd_client_id and src.gd_client_secret)
    has_sa = bool(src.gd_service_account_json)
    if not has_oauth and not has_sa:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The folder has no inline credentials to import",
        )

    now = int(time.time())
    cred = SyncCredential(
        company_id=user.company_id,
        kind="google_oauth_client" if has_oauth else "google_service_account",
        label=folder.display_name or folder.path.rsplit("/", 1)[-1],
        client_id=src.gd_client_id or "",
        client_secret=src.gd_client_secret or "",
        service_account_json=src.gd_service_account_json or "",
        refresh_token=src.gd_refresh_token or "",
        created_by=user.email,
        created_at=now,
        updated_at=now,
    )
    db.add(cred)
    db.flush()  # need cred.id for the re-point below

    if is_folder_owner(db, folder_id, user.id):
        # Owner import: the credential becomes the single source of truth —
        # re-point the folder and clear the now-duplicated inline fields.
        src.gd_credential_id = cred.id
        src.gd_client_id = None
        src.gd_client_secret = None
        src.gd_refresh_token = None
        src.gd_service_account_json = None

    db.commit()
    db.refresh(cred)
    return _to_out(db, cred)


@oauth_router.post(
    "/credentials/{cred_id}/google/auth", response_model=CredAuthInitOut
)
def credential_auth_init(
    cred_id: int,
    request: Request,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> CredAuthInitOut:
    """Google consent URL for an OAuth-client credential.

    Same callback URL as the folder-level flow (one GCP registration);
    the ``state`` carries ``cred:<id>`` so the callback stores the
    refresh token on the credential instead of a folder row.
    """
    cred = get_company_credential(db, user, cred_id)
    if cred.kind != "google_oauth_client":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only OAuth-client credentials use the consent flow",
        )
    if not (cred.client_id and cred.client_secret):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The credential is missing its client_id/client_secret",
        )
    state = base64.urlsafe_b64encode(
        f"{CRED_STATE_PREFIX}{cred.id}".encode()
    ).decode()
    auth_url = gd_get_auth_url(
        client_id=cred.client_id,
        redirect_uri=external_redirect_uri(
            request, "/api/sync/oauth/google/callback"
        ),
        state=state,
    )
    return CredAuthInitOut(auth_url=auth_url)
