"""SecretStore — secure credential storage for GitLab Personal Access Tokens.

============================= STUDENT WORK (§9.3) =============================
The starter ships ONLY this interface, wired into the repo-fetch code path
(app/core/ingestion/repo_fetch.py). Implementing real encryption-at-rest is
required student work.

Requirements your implementation must meet:

- The raw token must NEVER appear in plaintext at rest — not in the
  database (the `credentials` table ships for your use), not in config
  files, not in logs. Grading plants a known token and greps the running
  container's persisted state for it.
- A single stored PAT (credential_ref="gitlab-pat") is sufficient: GitLab
  PATs are account-scoped, so one token covers the personal lecture repo
  and any project repos the student can access.
- How you encrypt, where the key lives, and how you expose save/get to the
  UI is your design to make — and to justify.
===============================================================================

Design (T4):

- Cipher: AES-256-GCM. `encrypted_value` holds `nonce (12 bytes) ||
  ciphertext+tag`, with a fresh random nonce per save, so the same token
  never encrypts to the same bytes. The credential_ref is the associated
  data: a ciphertext copied onto another row fails to decrypt.
- Key: 32 bytes, never in the repo or the database. SECRET_STORE_KEY
  (urlsafe base64) wins when set; otherwise the first save generates a key
  file (SECRET_KEY_FILE, mode 0600) on its own volume shared by backend and
  worker. A database dump, a log or a repo clone alone can't recover the
  token. Someone with the host's Docker volumes holds both halves — accepted
  for a local, single-user app.
- Errors say what to do next and never include the token.
"""

import base64
import binascii
import os
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Credential

KEY_BYTES = 32
NONCE_BYTES = 12


class SecretStoreError(RuntimeError):
    """A credential can't be saved or read. The message is shown to the user,
    so it explains the fix and never contains the token."""


class MissingCredential(SecretStoreError):
    """No token is stored under the requested credential_ref."""


class SecretStore:
    def __init__(self, db: Session) -> None:
        self.db = db

    def save(self, credential_ref: str, token: str) -> None:
        """Encrypt `token` and store it under `credential_ref` (replacing any
        earlier one)."""
        token = token.strip()
        if not token:
            raise SecretStoreError("the token is empty; paste the full GitLab PAT")
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(_load_key(create=True)).encrypt(
            nonce, token.encode(), credential_ref.encode()
        )
        row = self._row(credential_ref)
        if row is None:
            row = Credential(credential_ref=credential_ref)
            self.db.add(row)
        row.encrypted_value = nonce + sealed
        self.db.commit()

    def get(self, credential_ref: str) -> str:
        """Return the decrypted token stored under `credential_ref`."""
        row = self._row(credential_ref)
        if row is None or not row.encrypted_value:
            raise MissingCredential(
                f"No GitLab PAT is stored (credential {credential_ref!r}). "
                "Add one in Settings, then re-ingest this repo."
            )
        key = _load_key(create=False)
        blob = row.encrypted_value
        try:
            plain = AESGCM(key).decrypt(
                blob[:NONCE_BYTES], blob[NONCE_BYTES:], credential_ref.encode()
            )
        except InvalidTag:
            raise SecretStoreError(
                f"The stored GitLab PAT (credential {credential_ref!r}) can't be "
                "decrypted with the current key. Re-enter it in Settings."
            ) from None
        return plain.decode()

    def stored_at(self, credential_ref: str) -> datetime | None:
        """When a token was last saved under `credential_ref`, or None if
        none is stored. Never decrypts: the UI only needs to know it exists."""
        row = self._row(credential_ref)
        if row is None or not row.encrypted_value:
            return None
        return row.updated_at or row.created_at

    def delete(self, credential_ref: str) -> bool:
        """Remove the stored token; returns whether there was one."""
        row = self._row(credential_ref)
        if row is None:
            return False
        self.db.delete(row)
        self.db.commit()
        return True

    def _row(self, credential_ref: str) -> Credential | None:
        return (
            self.db.query(Credential)
            .filter(Credential.credential_ref == credential_ref)
            .one_or_none()
        )


def _load_key(create: bool) -> bytes:
    """The 32-byte key: SECRET_STORE_KEY if set, else the key file.

    With `create`, a missing key file is generated (0600, created exclusively
    so the backend and worker can't each write a different one).
    """
    settings = get_settings()
    if settings.secret_store_key:
        return _decode_key(settings.secret_store_key, "SECRET_STORE_KEY")

    path = Path(settings.secret_key_file)
    if not path.exists():
        if not create:
            raise SecretStoreError(
                "The encryption key for stored credentials is missing. "
                "Re-enter the GitLab PAT in Settings."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = base64.urlsafe_b64encode(os.urandom(KEY_BYTES))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first; read theirs below
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(encoded)
    return _decode_key(path.read_text().strip(), str(path))


def _decode_key(encoded: str, where: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != KEY_BYTES:
        raise SecretStoreError(
            f"The encryption key in {where} is not {KEY_BYTES} bytes of urlsafe base64."
        )
    return key
