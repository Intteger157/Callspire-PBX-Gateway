"""GitHub-backed mobile release settings (IntermarkCaller APK proxy)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

_DB_PATH: Optional[Path] = None

_DEFAULTS: dict[str, str] = {
    "enabled": "false",
    "github_owner": "",
    "github_repo": "",
    "github_token": "",
    "asset_name": "IGCaller.apk",
}


def init_db(db_path: str | Path | None = None) -> None:
    global _DB_PATH
    if db_path is not None:
        _DB_PATH = Path(db_path)
    elif _DB_PATH is None:
        env_path = (os.environ.get("MOBILE_RELEASES_DB_PATH") or "").strip()
        _DB_PATH = Path(env_path) if env_path else Path(__file__).resolve().parent / "mobile_releases.sqlite"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mobile_release_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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


def _get(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM mobile_release_settings WHERE key = ?",
            (key,),
        ).fetchone()
    if row:
        return str(row["value"] or "")
    return _DEFAULTS.get(key, default)


def _set(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO mobile_release_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_config() -> dict[str, Any]:
    token = _resolve_token()
    return {
        "enabled": _get("enabled", "false").lower() in ("1", "true", "yes", "on"),
        "github_owner": _get("github_owner").strip(),
        "github_repo": _get("github_repo").strip(),
        "asset_name": _get("asset_name", "IGCaller.apk").strip() or "IGCaller.apk",
        "token_configured": bool(token),
        "token_source": _token_source(),
    }


def get_admin_config() -> dict[str, Any]:
    public = get_config()
    return {
        **public,
        "github_token": "",  # never return secret to clients
    }


def update_config(
    *,
    enabled: bool | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    github_token: str | None = None,
    asset_name: str | None = None,
) -> dict[str, Any]:
    if enabled is not None:
        _set("enabled", "true" if enabled else "false")
    if github_owner is not None:
        _set("github_owner", github_owner.strip())
    if github_repo is not None:
        _set("github_repo", github_repo.strip())
    if github_token is not None and github_token.strip():
        _set("github_token", github_token.strip())
    if asset_name is not None:
        _set("asset_name", asset_name.strip() or "IGCaller.apk")
    return get_admin_config()


def _token_source() -> str:
    if (os.environ.get("GITHUB_MOBILE_RELEASES_TOKEN") or "").strip():
        return "env"
    if _get("github_token").strip():
        return "db"
    return ""


def _resolve_token() -> str:
    env_token = (os.environ.get("GITHUB_MOBILE_RELEASES_TOKEN") or "").strip()
    if env_token:
        return env_token
    return _get("github_token").strip()


def is_ready() -> bool:
    cfg = get_config()
    return bool(
        cfg.get("enabled")
        and cfg.get("github_owner")
        and cfg.get("github_repo")
        and _resolve_token()
    )


def github_settings() -> dict[str, str]:
    cfg = get_config()
    return {
        "owner": str(cfg.get("github_owner") or ""),
        "repo": str(cfg.get("github_repo") or ""),
        "token": _resolve_token(),
        "asset_name": str(cfg.get("asset_name") or "IGCaller.apk"),
    }
