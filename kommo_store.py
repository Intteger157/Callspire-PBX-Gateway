"""Kommo OAuth and per-extension user mapping storage.

Delegates to permissions_db when available; otherwise uses a local SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import permissions_db as _permissions_db
except ImportError:  # pragma: no cover
    _permissions_db = None

_DB_PATH: Optional[Path] = None


def _use_permissions_db() -> bool:
    if _permissions_db is None:
        return False
    return bool(
        hasattr(_permissions_db, "get_kommo_oauth")
        or hasattr(_permissions_db, "get_kommo_integration")
        or hasattr(_permissions_db, "get_kommo_oauth_tokens")
    )


def init_db(db_path: str | Path | None = None) -> None:
    if _use_permissions_db():
        return
    global _DB_PATH
    if db_path is not None:
        _DB_PATH = Path(db_path)
    elif _DB_PATH is None:
        _DB_PATH = Path(__file__).resolve().parent / "kommo_store.sqlite"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kommo_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kommo_extension_users (
                extension TEXT PRIMARY KEY,
                kommo_user_id INTEGER,
                kommo_user_name TEXT,
                excluded INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        init_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _local_get(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM kommo_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def _local_set(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kommo_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def is_enabled() -> bool:
    if _use_permissions_db():
        return bool(_permissions_db.get_kommo_enabled())  # type: ignore[union-attr]
    if _local_get("enabled", "0") == "1":
        return True
    oauth = get_oauth_tokens()
    return bool((oauth.get("access_token") or "").strip())


def set_enabled(enabled: bool) -> None:
    if _use_permissions_db():
        _permissions_db.set_kommo_enabled(enabled)  # type: ignore[union-attr]
        return
    _local_set("enabled", "1" if enabled else "0")


def get_subdomain() -> str:
    if _permissions_db is not None and hasattr(_permissions_db, "get_kommo_integration"):
        cfg = _permissions_db.get_kommo_integration() or {}  # type: ignore[union-attr]
        return (cfg.get("subdomain") or "").strip()
    if _use_permissions_db() and hasattr(_permissions_db, "get_kommo_subdomain"):
        return (_permissions_db.get_kommo_subdomain() or "").strip()  # type: ignore[union-attr]
    return _local_get("subdomain", "").strip()


def set_subdomain(subdomain: str) -> None:
    if _use_permissions_db():
        _permissions_db.set_kommo_subdomain(subdomain)  # type: ignore[union-attr]
        return
    _local_set("subdomain", subdomain.strip())


def get_oauth_tokens() -> dict[str, Any]:
    if _permissions_db is not None and hasattr(_permissions_db, "get_kommo_oauth_tokens"):
        return _permissions_db.get_kommo_oauth_tokens() or {}  # type: ignore[union-attr]
    if _use_permissions_db() and hasattr(_permissions_db, "get_kommo_oauth"):
        return _permissions_db.get_kommo_oauth() or {}  # type: ignore[union-attr]
    raw = _local_get("oauth_json", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def set_oauth_tokens(
    access_token: str,
    refresh_token: str,
    expires_at: Optional[str],
    *,
    client_id: str = "",
    client_secret: str = "",
) -> None:
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at or "",
        "client_id": client_id,
        "client_secret": client_secret,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if _use_permissions_db():
        _permissions_db.set_kommo_oauth(data)  # type: ignore[union-attr]
        return
    _local_set("oauth_json", json.dumps(data))


def get_extension_mapping(extension: str) -> dict[str, Any]:
    ext = (extension or "").strip()
    if _use_permissions_db() and hasattr(_permissions_db, "get_kommo_extension_user"):
        return _permissions_db.get_kommo_extension_user(ext) or {}  # type: ignore[union-attr]
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_extension_users WHERE extension = ?", (ext,)
        ).fetchone()
    if not row:
        return {"extension": ext, "excluded": False}
    return {
        "extension": ext,
        "kommo_user_id": row["kommo_user_id"],
        "kommo_user_name": row["kommo_user_name"],
        "excluded": bool(row["excluded"]),
    }


def set_extension_mapping(
    extension: str,
    kommo_user_id: Optional[int],
    kommo_user_name: str = "",
    excluded: bool = False,
) -> None:
    ext = (extension or "").strip()
    if _use_permissions_db() and hasattr(_permissions_db, "set_kommo_extension_user"):
        _permissions_db.set_kommo_extension_user(  # type: ignore[union-attr]
            ext, kommo_user_id, kommo_user_name, excluded=excluded
        )
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kommo_extension_users(extension, kommo_user_id, kommo_user_name, excluded)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(extension) DO UPDATE SET
                kommo_user_id = excluded.kommo_user_id,
                kommo_user_name = excluded.kommo_user_name,
                excluded = excluded.excluded
            """,
            (ext, kommo_user_id, kommo_user_name, 1 if excluded else 0),
        )
        conn.commit()


def token_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= datetime.now(timezone.utc)
    except ValueError:
        return False


def build_account_base_url(subdomain: str) -> str:
    sub = (subdomain or "").strip().lower()
    if not sub:
        return ""
    if ".kommo.com" in sub or ".amocrm." in sub:
        return f"https://{sub.rstrip('/')}"
    return f"https://{sub}.kommo.com"
