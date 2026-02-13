from __future__ import annotations

import hmac
import secrets

try:
    import bcrypt as _bcrypt
except ModuleNotFoundError:  # pragma: no cover - exercised when bcrypt wheel is unavailable
    _bcrypt = None
    import crypt

_BCRYPT_ALPHABET = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _bcrypt_salt(rounds: int = 12) -> str:
    token = "".join(secrets.choice(_BCRYPT_ALPHABET) for _ in range(22))
    return f"$2b${rounds:02d}${token}"


def hash_password(password: str) -> str:
    if _bcrypt is not None:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    digest = crypt.crypt(password, _bcrypt_salt())
    if not digest or not digest.startswith("$2"):
        raise RuntimeError("bcrypt hashing is not available in this runtime")
    return digest


def verify_password(password: str, password_hash: str) -> bool:
    if _bcrypt is not None:
        return _bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    candidate = crypt.crypt(password, password_hash)
    return bool(candidate) and hmac.compare_digest(candidate, password_hash)
