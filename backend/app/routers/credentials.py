"""Entering, replacing and removing the GitLab PAT (T4).

The token only ever travels inward: PUT hands it to SecretStore, which
encrypts it. No response, log line or error message carries it back out —
GET reports whether one is stored and when, nothing more.
"""

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.secret_store import SecretStore, SecretStoreError
from app.database import get_db

router = APIRouter(prefix="/api", tags=["credentials"])

# Same shape as Resource.credential_ref (String(64)).
_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CredentialStatus(BaseModel):
    credential_ref: str
    stored: bool
    updated_at: datetime | None


def _check_ref(credential_ref: str) -> str:
    if not _REF.match(credential_ref):
        raise HTTPException(
            422, "credential_ref must be lowercase letters, digits, - or _"
        )
    return credential_ref


@router.get("/credentials/{credential_ref}")
def credential_status(
    credential_ref: str, db: Session = Depends(get_db)
) -> CredentialStatus:
    ref = _check_ref(credential_ref)
    stored_at = SecretStore(db).stored_at(ref)
    return CredentialStatus(
        credential_ref=ref, stored=stored_at is not None, updated_at=stored_at
    )


@router.put("/credentials/{credential_ref}")
async def save_credential(
    credential_ref: str, request: Request, db: Session = Depends(get_db)
) -> CredentialStatus:
    ref = _check_ref(credential_ref)
    # Parsed by hand, not as a pydantic body: FastAPI's 422 for a malformed
    # body echoes the raw input, which here would be the token.
    try:
        body = await request.json()
    except ValueError:
        body = None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str):
        raise HTTPException(422, 'expected a JSON body {"token": "<your GitLab PAT>"}')
    store = SecretStore(db)
    try:
        store.save(ref, token)
    except SecretStoreError as exc:
        # SecretStore's messages never contain the token.
        raise HTTPException(422, str(exc)) from None
    return CredentialStatus(
        credential_ref=ref, stored=True, updated_at=store.stored_at(ref)
    )


@router.delete("/credentials/{credential_ref}", status_code=204)
def delete_credential(credential_ref: str, db: Session = Depends(get_db)) -> None:
    ref = _check_ref(credential_ref)
    if not SecretStore(db).delete(ref):
        raise HTTPException(404, "no credential stored under that name")
