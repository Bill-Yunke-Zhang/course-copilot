from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Credential(Base):
    """Storage row for SecretStore (app/core/secret_store.py).

    The table ships in the starter; the encryption logic does not — see the
    SecretStore stub. GRADING NOTE: the raw token must never appear in
    plaintext anywhere at rest (this table, config files, or logs). Grading
    plants a known PAT and greps the container's persisted state for it.
    """

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    credential_ref: Mapped[str] = mapped_column(String(64), unique=True)
    # Ciphertext only. If your implementation needs more columns (salt,
    # nonce, key-derivation parameters…), add them with a migration.
    encrypted_value: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
