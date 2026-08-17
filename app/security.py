"""Password hashing and at-rest encryption for Dhan credentials.

Dhan access tokens are secrets equivalent to a login session for the user's
brokerage account — they are never logged and never stored in plaintext.

Uses the `bcrypt` package directly rather than passlib — passlib (last
released 2020) breaks under bcrypt>=4.1 (its self-test assumes bcrypt's old,
looser 72-byte handling), which forced pinning bcrypt to an exact old
version. That old bcrypt then has no prebuilt wheel for newer Python
releases and would need a Rust toolchain to build from source. Calling
bcrypt directly sidesteps the whole problem — no passlib, no version pin
tightrope.
"""

from __future__ import annotations

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

# bcrypt silently ignores any bytes beyond 72 — reject overlong passwords
# explicitly at the call site instead (see app/routers/auth.py) rather than
# let them be quietly truncated.
_MAX_PASSWORD_BYTES = 72


def hash_password(plain_password: str) -> str:
    password_bytes = plain_password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, password_hash: str) -> bool:
    password_bytes = plain_password.encode("utf-8")[:_MAX_PASSWORD_BYTES]
    try:
        return bcrypt.checkpw(password_bytes, password_hash.encode("ascii"))
    except ValueError:
        # Malformed/foreign hash format — never a valid match.
        return False


def _fernet() -> Fernet:
    key = get_settings().credentials_encryption_key
    if not key:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and put it in your .env file."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plain_text: str) -> str:
    return _fernet().encrypt(plain_text.encode()).decode()


def decrypt_secret(cipher_text: str) -> str:
    try:
        return _fernet().decrypt(cipher_text.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored credential could not be decrypted — it may be corrupt or the encryption key changed.") from exc
