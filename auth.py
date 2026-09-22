"""Authentication module for MikoPBX CDR Proxy.

Supports two authentication paths:
1. Admin — username/bcrypt-hash from config.yaml ``users`` list.
2. MikoPBX user — extension + plaintext SIP password from ``m_Sip.secret``.
3. App user — email/bcrypt-hash from the proxy permissions DB, mapped to a MikoPBX extension.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

import permissions_db


# --------------- Admin auth (config.yaml users, bcrypt) ---------------

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def authenticate_user(username: str, password: str, users: list[dict]) -> dict | None:
    """Authenticate against the ``users`` list in config.yaml (bcrypt hashes)."""
    for u in users:
        if u["username"] == username and verify_password(password, u["password_hash"]):
            return {
                **u,
                "role": "admin",
                "must_change_password": bool(u.get("must_change_password")),
            }
    return None


# --------------- MikoPBX user auth (m_Sip.secret, plaintext) ---------------

def authenticate_mikopbx_user(
    extension: str, password: str, config_db_path: str
) -> dict | None:
    """Authenticate a MikoPBX internal extension against ``m_Sip.secret``.

    Returns a dict with ``extension``, ``name``, ``role`` on success, or *None*.
    """
    try:
        conn = sqlite3.connect(f"file:{config_db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT extension, secret, description FROM m_Sip "
            "WHERE type='peer' AND extension=? AND disabled='0'",
            (extension,),
        ).fetchone()
        conn.close()
    except Exception:
        return None

    if row is None or row[1] != password:
        return None

    return {
        "username": row[0],
        "extension": row[0],
        "name": row[2] or row[0],
        "role": "user",
    }


# --------------- App user auth (email in permissions.db) ---------------

def authenticate_app_user(email: str, password: str) -> dict | None:
    """Authenticate a web/app user by email and return mapped extension."""
    u = permissions_db.get_app_user_by_email(email)
    if u is None:
        return None
    if u.get("disabled"):
        return None
    if not verify_password(password, u["password_hash"]):
        return None

    permissions_db.mark_app_user_login(u["email"])
    return {
        "email": u["email"],
        "extension": u["mikopbx_extension"],
        "role": "user",
        "must_change_password": bool(u.get("must_change_password")),
    }


# --------------- JWT ---------------

def create_jwt(
    username: str,
    secret: str,
    expire_days: int = 30,
    role: str = "user",
    name: str | None = None,
    extension: str | None = None,
    must_change_password: bool | None = None,
) -> str:
    payload: dict = {
        "sub": username,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=expire_days),
    }
    if name:
        payload["name"] = name
    if extension:
        payload["ext"] = extension
    if must_change_password is not None:
        payload["must_change_password"] = bool(must_change_password)
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_jwt(token: str, secret: str) -> dict | None:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
