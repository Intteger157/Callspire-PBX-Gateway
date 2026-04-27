import os
import secrets
import yaml
import bcrypt
from pathlib import Path
import glob

CONFIG_PATH = (
    os.environ.get("PBX_GATEWAY_CONFIG")
    or os.environ.get("CDR_PROXY_CONFIG")
    or "config.yaml"
)

_defaults = {
    "host": "0.0.0.0",
    "port": 8443,
    "jwt_secret": secrets.token_hex(32),
    "jwt_expire_days": 30,
    # Optional extra protection for internet-exposed deployments:
    # If set, you can optionally require header X-Callspire-Service-Token for some login flows.
    "service_token": "",
    # Modes:
    # - "off": do not require service token
    # - "email_only": require service token only when logging in with an email (app users)
    # - "all": require service token for any password-based login
    "service_token_mode": "off",
    "cdr_db_path": "/var/spool/mikopbx/storage/usbdisk1/mikopbx/astlogs/asterisk/cdr.db",
    # If set (path *inside* the MikoPBX container), CDR is read via ``docker cp`` to a temp
    # file — use when the host ``cdr_db_path`` is stale/empty but Asterisk writes SQLite
    # only inside the container (common bind-mount mismatch).
    "cdr_docker_db_path": "",
    "config_db_path": "/var/spool/mikopbx/cf/conf/mikopbx.db",
    "recording_base": "/var/spool/mikopbx",
    # Source of plaintext SIP secrets for browser WebRTC (MikoPBX encrypts m_Sip.secret
    # in the DB, but writes the real password into pjsip.conf at config-regen time).
    # If ``mikopbx_docker_container`` is non-empty, the proxy runs ``docker exec <c> cat <path>``
    # to read the file; otherwise it reads ``mikopbx_pjsip_conf_path`` from the host directly.
    "mikopbx_pjsip_conf_path": "/etc/asterisk/pjsip.conf",
    "mikopbx_docker_container": "mikopbx",
    "mikopbx_pjsip_cache_seconds": 60,
    # --- MikoPBX REST API v3 (optional; when configured the proxy prefers it
    #     over direct SQLite for CDR, extensions and trunk lookups). ---
    # Base URL of the MikoPBX web UI (http://127.0.0.1 when the proxy runs on
    # the same host, https://pbx.example.com for external access).
    "mikopbx_rest_url": "",
    # Long-lived API key (JWT) generated in MikoPBX UI: Settings -> API Keys.
    # Preferred over login/password: no re-login, survives PBX restarts.
    "mikopbx_api_key": "",
    # Password fallback when no API key is configured. Access token lasts ~15
    # minutes; the REST client refreshes it automatically on 401.
    "mikopbx_admin_login": "",
    "mikopbx_admin_password": "",
    # Skip TLS verification for self-signed MikoPBX certificates on LAN.
    "mikopbx_verify_ssl": True,
    "mikopbx_rest_timeout_seconds": 15,
    # Master switch. When False we keep the legacy SQLite + docker cp path
    # (so the proxy stays bit-for-bit backwards-compatible on existing hosts).
    # Turn on per-PBX once the API key / admin creds are verified.
    "use_rest_api": False,
    "ssl_certfile": None,
    "ssl_keyfile": None,
    # Public URL the admin panel embeds in ``callspire://provision`` links.
    # Leave empty to default to the admin-panel's own origin when the admin
    # clicks "Generate link" — only set this when the admin panel lives
    # behind a reverse-proxy whose externally visible hostname differs from
    # what the admin ``Host`` header sees.
    "public_url": "",
    "users": [],
}

def _first_existing_file(candidates: list[str]) -> str | None:
    for p in candidates:
        if not p:
            continue
        try:
            if Path(p).is_file():
                return p
        except OSError:
            continue
    return None


def _first_existing_dir(candidates: list[str]) -> str | None:
    for p in candidates:
        if not p:
            continue
        try:
            if Path(p).is_dir():
                return p
        except OSError:
            continue
    return None


def _autofix_mikopbx_paths(cfg: dict) -> dict:
    """Best-effort path recovery for common MikoPBX layouts.

    This prevents hard failures when:
    - volume names differ (e.g. mikopbx_storage vs mikopbx_storage1),
    - the proxy is moved between "native" (/var/spool/mikopbx) and docker-volume layouts,
    - config.yaml was edited but the underlying filesystem differs.
    """
    # --- config db (mikopbx.db) ---
    config_candidates = [
        str((cfg.get("config_db_path") or "").strip()),
        _defaults["config_db_path"],
        "/var/lib/docker/volumes/mikopbx_cf/_data/conf/mikopbx.db",
        "/var/lib/docker/volumes/mikopbx_cf/_data/mikopbx.db",
    ]
    # Fallback glob: any docker volume containing /conf/mikopbx.db
    config_candidates += glob.glob("/var/lib/docker/volumes/*/_data/**/conf/mikopbx.db", recursive=True)[:50]
    found_config = _first_existing_file([c for c in config_candidates if c])
    if found_config and found_config != cfg.get("config_db_path"):
        print(f"[config] WARNING: config_db_path not found, using: {found_config}")
        cfg["config_db_path"] = found_config

    # --- cdr db (cdr.db) ---
    cdr_candidates = [
        str((cfg.get("cdr_db_path") or "").strip()),
        _defaults["cdr_db_path"],
        "/var/lib/docker/volumes/mikopbx_storage/_data/usbdisk1/mikopbx/astlogs/asterisk/cdr.db",
        "/var/lib/docker/volumes/mikopbx_storage/_data/mikopbx/astlogs/asterisk/cdr.db",
    ]
    cdr_candidates += glob.glob("/var/lib/docker/volumes/*/_data/**/astlogs/asterisk/cdr.db", recursive=True)[:50]
    found_cdr = _first_existing_file([c for c in cdr_candidates if c])
    if found_cdr and found_cdr != cfg.get("cdr_db_path"):
        print(f"[config] WARNING: cdr_db_path not found, using: {found_cdr}")
        cfg["cdr_db_path"] = found_cdr

    # --- recording base ---
    rec_candidates = [
        str((cfg.get("recording_base") or "").strip()),
        _defaults["recording_base"],
        "/var/lib/docker/volumes/mikopbx_storage/_data",
        "/var/spool/mikopbx",
    ]
    found_rec = _first_existing_dir([c for c in rec_candidates if c])
    if found_rec and found_rec != cfg.get("recording_base"):
        print(f"[config] WARNING: recording_base not found, using: {found_rec}")
        cfg["recording_base"] = found_rec

    return cfg


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    for key, default in _defaults.items():
        cfg.setdefault(key, default)

    # Best-effort path recovery (does not touch docker-based CDR reads; only host paths).
    cfg = _autofix_mikopbx_paths(cfg)

    if not cfg["users"]:
        default_password = secrets.token_urlsafe(16)
        cfg["users"] = [
            {"username": "admin", "password_hash": _hash_password(default_password)}
        ]
        print(f"[config] No users configured. Created default user:")
        print(f"         username: admin")
        print(f"         password: {default_password}")
        print(f"         Save this password! It will not be shown again.")
        _save_config(cfg)

    return cfg


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
