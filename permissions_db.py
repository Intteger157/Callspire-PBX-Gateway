"""Local SQLite database for CallerID permissions, number labels and AMI configuration.

Stores which CallerID numbers each MikoPBX extension is allowed to use,
human-readable names/notes for trunk numbers, and the AMI connection
credentials used by the proxy to issue Originate commands.
"""

import json
import os
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

_DB_PATH: str | None = None


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist.  Call once at startup."""
    global _DB_PATH
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "permissions.db")
    _DB_PATH = db_path

    conn = sqlite3.connect(_DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS callerid_permissions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            extension TEXT    NOT NULL,
            callerid  TEXT    NOT NULL,
            UNIQUE(extension, callerid)
        );

        CREATE TABLE IF NOT EXISTS ami_config (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            host     TEXT    DEFAULT '127.0.0.1',
            port     INTEGER DEFAULT 5038,
            username TEXT    DEFAULT '',
            secret   TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS callerid_names (
            number TEXT PRIMARY KEY,
            name   TEXT NOT NULL DEFAULT '',
            note   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS trunk_manual_callerids (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            trunk_uniqid TEXT NOT NULL,
            callerid     TEXT NOT NULL,
            UNIQUE(trunk_uniqid, callerid)
        );

        CREATE TABLE IF NOT EXISTS app_users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            email                TEXT    NOT NULL UNIQUE,
            password_hash        TEXT    NOT NULL,
            mikopbx_extension    TEXT    NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            disabled             INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL,
            last_login_at        TEXT    NULL
        );

        INSERT OR IGNORE INTO ami_config (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS websoftphone_config (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            ws_url   TEXT NOT NULL DEFAULT '',
            sip_host TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO websoftphone_config (id) VALUES (1);

        -- One-time tokens for auto-provisioning the WPF softphone (the admin
        -- generates a URL like ``callspire://provision?token=<token_id>`` and
        -- sends it to the end user; the desktop app redeems the token once to
        -- fetch SIP credentials straight from MikoPBX, without the user ever
        -- typing a password manually).
        --
        -- The ``token_id`` is the random opaque value embedded in the URL. We
        -- deliberately do NOT store the extension's SIP secret here; the
        -- secret is fetched from MikoPBX on redeem so a compromise of this
        -- file alone is harmless.
        CREATE TABLE IF NOT EXISTS provision_tokens (
            token_id    TEXT    PRIMARY KEY,
            extension   TEXT    NOT NULL,
            created_by  TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL,
            expires_at  TEXT    NOT NULL,
            used_at     TEXT    NULL
        );
        CREATE INDEX IF NOT EXISTS idx_provision_tokens_ext ON provision_tokens(extension);

        -- Per-user preferences for the browser softphone (audio devices,
        -- AEC/NS/AGC toggles, ringtone device, etc.). Stored as a JSON blob
        -- keyed by the user's e-mail so the schema can evolve without DB
        -- migrations. The server enforces a strict whitelist of keys on
        -- write (see set_user_prefs) and never accepts secrets.
        CREATE TABLE IF NOT EXISTS user_prefs (
            email      TEXT PRIMARY KEY,
            prefs_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _conn() -> sqlite3.Connection:
    assert _DB_PATH, "permissions_db.init_db() must be called first"
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------- CallerID permissions ---------------

def get_allowed_callerids(extension: str) -> list[str]:
    conn = _conn()
    rows = conn.execute(
        "SELECT callerid FROM callerid_permissions WHERE extension = ? ORDER BY callerid",
        (extension,),
    ).fetchall()
    conn.close()
    return [r["callerid"] for r in rows]


def set_allowed_callerids(extension: str, callerids: list[str]) -> None:
    """Replace all CallerID permissions for *extension*."""
    conn = _conn()
    conn.execute("DELETE FROM callerid_permissions WHERE extension = ?", (extension,))
    for cid in callerids:
        conn.execute(
            "INSERT OR IGNORE INTO callerid_permissions (extension, callerid) VALUES (?, ?)",
            (extension, cid),
        )
    conn.commit()
    conn.close()


def get_all_permissions() -> list[dict]:
    """Return ``[{extension, callerids: [...]}, ...]`` for every extension that has at least one."""
    conn = _conn()
    rows = conn.execute(
        "SELECT extension, callerid FROM callerid_permissions ORDER BY extension, callerid"
    ).fetchall()
    conn.close()

    by_ext: dict[str, list[str]] = {}
    for r in rows:
        by_ext.setdefault(r["extension"], []).append(r["callerid"])
    return [{"extension": ext, "callerids": cids} for ext, cids in by_ext.items()]


# --------------- CallerID names / labels ---------------

def get_all_callerid_names() -> dict[str, dict]:
    """Return ``{number: {name, note}, ...}`` for all labelled numbers."""
    conn = _conn()
    rows = conn.execute("SELECT number, name, note FROM callerid_names ORDER BY number").fetchall()
    conn.close()
    return {r["number"]: {"name": r["name"], "note": r["note"]} for r in rows}


def set_callerid_name(number: str, name: str, note: str = "") -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO callerid_names (number, name, note) VALUES (?, ?, ?) "
        "ON CONFLICT(number) DO UPDATE SET name=excluded.name, note=excluded.note",
        (number, name, note),
    )
    conn.commit()
    conn.close()


def delete_callerid_name(number: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM callerid_names WHERE number = ?", (number,))
    conn.commit()
    conn.close()


# --------------- Manual CallerIDs per SIP trunk (contract DIDs) ---------------

def add_trunk_manual_callerid(trunk_uniqid: str, callerid: str) -> None:
    trunk_uniqid = (trunk_uniqid or "").strip()
    callerid = (callerid or "").strip()
    if not trunk_uniqid or not callerid:
        raise ValueError("trunk_uniqid and callerid are required")
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO trunk_manual_callerids (trunk_uniqid, callerid) VALUES (?, ?)",
        (trunk_uniqid, callerid),
    )
    conn.commit()
    conn.close()


def remove_trunk_manual_callerid(trunk_uniqid: str, callerid: str) -> None:
    conn = _conn()
    conn.execute(
        "DELETE FROM trunk_manual_callerids WHERE trunk_uniqid = ? AND callerid = ?",
        (trunk_uniqid, callerid),
    )
    conn.commit()
    conn.close()


def get_manual_callerids_by_trunk() -> dict[str, list[str]]:
    """``{trunk_uniqid: [callerid, ...], ...}`` sorted."""
    conn = _conn()
    rows = conn.execute(
        "SELECT trunk_uniqid, callerid FROM trunk_manual_callerids ORDER BY trunk_uniqid, callerid"
    ).fetchall()
    conn.close()
    by_trunk: dict[str, list[str]] = {}
    for r in rows:
        by_trunk.setdefault(r["trunk_uniqid"], []).append(r["callerid"])
    return by_trunk


def get_all_manual_trunk_callerid_numbers() -> list[str]:
    """Distinct manual numbers (for merging into global CallerID picker)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT callerid FROM trunk_manual_callerids ORDER BY callerid"
    ).fetchall()
    conn.close()
    return [r["callerid"] for r in rows]


# --------------- AMI configuration ---------------

def get_ami_config() -> dict:
    conn = _conn()
    row = conn.execute("SELECT host, port, username, secret FROM ami_config WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"host": "127.0.0.1", "port": 5038, "username": "", "secret": ""}
    return dict(row)


def set_ami_config(host: str, port: int, username: str, secret: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE ami_config SET host = ?, port = ?, username = ?, secret = ? WHERE id = 1",
        (host, port, username, secret),
    )
    conn.commit()
    conn.close()


# --------------- Web softphone WebRTC (public URLs, no secrets) ---------------

def get_webrtc_public_config() -> dict:
    conn = _conn()
    row = conn.execute("SELECT ws_url, sip_host FROM websoftphone_config WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"ws_url": "", "sip_host": ""}
    return {"ws_url": row["ws_url"] or "", "sip_host": row["sip_host"] or ""}


def set_webrtc_public_config(ws_url: str, sip_host: str) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE websoftphone_config SET ws_url = ?, sip_host = ? WHERE id = 1",
        (ws_url or "", sip_host or ""),
    )
    conn.commit()
    conn.close()


# --------------- App users (email login) ---------------

def find_other_app_user_with_mikopbx_extension(
    mikopbx_extension: str, *, exclude_email: str | None = None
) -> str | None:
    """If a different app user already has this Miko extension, return that user's email. Otherwise None.

    No two /softphone logins may share the same MikoPBX extension mapping.
    """
    ext = (mikopbx_extension or "").strip()
    if not ext:
        return None
    ex = (exclude_email or "").strip().lower()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT email FROM app_users WHERE TRIM(mikopbx_extension) = ?",
            (ext,),
        ).fetchall()
        for r in rows:
            e = (r[0] or "").strip()
            if not e:
                continue
            if e.lower() != ex:
                return e
        return None
    finally:
        conn.close()


def create_app_user(email: str, password_hash: str, mikopbx_extension: str, must_change_password: bool = True) -> dict:
    email = (email or "").strip().lower()
    mikopbx_extension = (mikopbx_extension or "").strip()
    if not email or not mikopbx_extension:
        raise ValueError("email and mikopbx_extension are required")
    if "@" not in email:
        raise ValueError("email must look like an email address")
    if not password_hash:
        raise ValueError("password_hash is required")

    other = find_other_app_user_with_mikopbx_extension(mikopbx_extension, exclude_email=None)
    if other:
        raise ValueError(
            f"MikoPBX extension {mikopbx_extension} is already linked to {other}. "
            "Change that account or use another extension."
        )

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO app_users (email, password_hash, mikopbx_extension, must_change_password, disabled, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (email, password_hash, mikopbx_extension, 1 if must_change_password else 0, _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, email, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users WHERE email = ?",
            (email,),
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def get_app_user_by_email(email: str) -> dict | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users WHERE email = ?",
            (email,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_app_users() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, email, mikopbx_extension, must_change_password, disabled, created_at, last_login_at "
            "FROM app_users ORDER BY email"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_app_user_password(email: str, password_hash: str, must_change_password: bool) -> None:
    email = (email or "").strip().lower()
    if not email or not password_hash:
        raise ValueError("email and password_hash are required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET password_hash = ?, must_change_password = ? WHERE email = ?",
            (password_hash, 1 if must_change_password else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def set_app_user_must_change_password(email: str, must_change_password: bool) -> None:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET must_change_password = ? WHERE email = ?",
            (1 if must_change_password else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def set_app_user_disabled(email: str, disabled: bool) -> None:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET disabled = ? WHERE email = ?",
            (1 if disabled else 0, email),
        )
        conn.commit()
    finally:
        conn.close()


def update_app_user_mikopbx_extension(email: str, mikopbx_extension: str) -> bool:
    """Re-point a web softphone login to a different MikoPBX extension. Returns True if a row was updated."""
    email = (email or "").strip().lower()
    mikopbx_extension = (mikopbx_extension or "").strip()
    if not email or not mikopbx_extension:
        raise ValueError("email and mikopbx_extension are required")
    other = find_other_app_user_with_mikopbx_extension(mikopbx_extension, exclude_email=email)
    if other:
        raise ValueError(
            f"MikoPBX extension {mikopbx_extension} is already linked to {other}."
        )
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE app_users SET mikopbx_extension = ? WHERE email = ?",
            (mikopbx_extension, email),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_app_user_login(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    conn = _conn()
    try:
        conn.execute(
            "UPDATE app_users SET last_login_at = ? WHERE email = ?",
            (_now_iso(), email),
        )
        conn.commit()
    finally:
        conn.close()


# --------------- Provisioning tokens (callspire:// auto-setup) ---------------

def create_provision_token(
    extension: str,
    *,
    ttl_minutes: int = 30,
    created_by: str = "",
) -> dict:
    """Generate a one-time token the admin hands to the end user.

    The returned ``token_id`` is random (256 bits of entropy) and gets
    embedded in a ``callspire://provision?token=...&proxy=...`` URL. The
    token becomes invalid after :func:`consume_provision_token` is called
    for it, or once ``expires_at`` elapses.
    """
    extension = (extension or "").strip()
    if not extension:
        raise ValueError("extension is required")
    ttl_minutes = max(1, min(int(ttl_minutes), 60 * 24))
    token_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO provision_tokens (token_id, extension, created_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_id, extension, created_by or "", now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "token_id": token_id,
        "extension": extension,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "ttl_minutes": ttl_minutes,
    }


def consume_provision_token(token_id: str) -> dict | None:
    """Atomically mark ``token_id`` as used and return its row, or None.

    The function returns ``None`` in three distinct error cases — token
    unknown, token expired, token already used — so callers should not
    leak which case occurred to avoid token-oracle attacks.
    """
    token_id = (token_id or "").strip()
    if not token_id:
        return None
    conn = _conn()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT token_id, extension, created_by, created_at, expires_at, used_at "
            "FROM provision_tokens WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        if row is None:
            return None
        if row["used_at"]:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return None
        if expires < datetime.now(timezone.utc):
            return None
        conn.execute(
            "UPDATE provision_tokens SET used_at = ? WHERE token_id = ? AND used_at IS NULL",
            (_now_iso(), token_id),
        )
        if conn.total_changes != 1:
            # Race: someone else redeemed between SELECT and UPDATE.
            return None
        conn.commit()
        return {
            "token_id": row["token_id"],
            "extension": row["extension"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
    finally:
        conn.close()


# --------------- Per-user softphone preferences ---------------

# Only these keys are accepted on write. Everything else is silently dropped.
# Keep this list in sync with the web client's preference shape.
_USER_PREF_STRING_KEYS = {
    "micId",
    "speakerId",
    "ringtoneDeviceId",
}
_USER_PREF_BOOL_KEYS = {
    "aec",
    "ns",
    "agc",
    "ringtoneEnabled",
}


def _sanitize_user_prefs(value: dict) -> dict:
    """Whitelist + type-coerce the incoming preferences dict.

    Anything not in the allowed key set is dropped. Strings are trimmed and
    capped so a compromised client cannot stuff megabytes into the DB.
    """
    if not isinstance(value, dict):
        raise ValueError("prefs must be a JSON object")
    out: dict = {}
    for k in _USER_PREF_STRING_KEYS:
        v = value.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        # MediaDeviceInfo.deviceId is an opaque base64 token; cap to 512 chars.
        out[k] = v[:512]
    for k in _USER_PREF_BOOL_KEYS:
        if k not in value:
            continue
        v = value.get(k)
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = bool(v)
        elif isinstance(v, str):
            out[k] = v.lower() in ("1", "true", "yes", "on")
    return out


def get_user_prefs(email: str) -> dict:
    """Return the stored preferences dict for *email*, or ``{}`` if none."""
    email = (email or "").strip().lower()
    if not email:
        return {}
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT prefs_json FROM user_prefs WHERE email = ?", (email,)
        ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["prefs_json"] or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Re-sanitize on read so even if the DB was poisoned offline we never
        # return unexpected keys.
        return _sanitize_user_prefs(data)
    finally:
        conn.close()


def set_user_prefs(email: str, patch: dict) -> dict:
    """Merge *patch* into the stored preferences for *email* and return the result.

    Unknown keys are ignored. Call with ``{"micId": None}`` to clear a field
    (or use an empty string) — the resulting dict simply omits the key.
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    if not isinstance(patch, dict):
        raise ValueError("prefs patch must be an object")

    current = get_user_prefs(email)
    # Allow clearing via explicit None/empty-string.
    for k in list(patch.keys()):
        v = patch[k]
        if v is None or v == "":
            current.pop(k, None)
            patch.pop(k)
    clean = _sanitize_user_prefs(patch)
    current.update(clean)

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO user_prefs (email, prefs_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET prefs_json = excluded.prefs_json, "
            "updated_at = excluded.updated_at",
            (email, json.dumps(current, separators=(",", ":")), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return current


def delete_user_prefs(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    conn = _conn()
    try:
        conn.execute("DELETE FROM user_prefs WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_provision_tokens(keep_used_days: int = 30) -> int:
    """Delete tokens that are expired AND either unused or used long ago.

    Called opportunistically; returns the number of rows removed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(keep_used_days)))).isoformat()
    now = _now_iso()
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM provision_tokens "
            "WHERE (used_at IS NOT NULL AND used_at < ?) "
            "   OR (used_at IS NULL AND expires_at < ?)",
            (cutoff, now),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
