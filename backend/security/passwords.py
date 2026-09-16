from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError


_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError, VerificationError):
        return False
