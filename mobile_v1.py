"""Mobile REST API v1 for Intermark softphone.

Endpoints:
  GET  /v1/health
  POST /v1/auth/login
  POST /v1/auth/refresh

Wraps existing MikoPBX auth (extension + SIP secret) and returns the mobile
provisioning bundle expected by the React Native client.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth import authenticate_app_user, authenticate_mikopbx_user, create_jwt, decode_jwt
import permissions_db
from pjsip_secrets import get_peer_secret as get_pjsip_peer_secret
from miko_rest_client import MikoRestClient, MikoRestError

router = APIRouter(prefix="/v1", tags=["mobile-v1"])


class MobileDeviceInfo(BaseModel):
    platform: str = ""
    device_id: str = ""
    app_version: str = ""
    push_token: str | None = None


class MobileLoginRequest(BaseModel):
    username: str
    password: str
    device: MobileDeviceInfo | None = None


class MobileRefreshRequest(BaseModel):
    refresh_token: str = Field(default="")


def _jwt_expire_seconds(jwt_expire_days_fn: Callable[[], int]) -> int:
    days = jwt_expire_days_fn()
    if days <= 0:
        return 10 * 365 * 24 * 3600
    return days * 24 * 3600


def _resolve_sip_host(cfg: dict[str, Any]) -> str:
    wrtc = permissions_db.get_webrtc_public_config()
    sip_host = (wrtc.get("sip_host") or "").strip()
    if sip_host:
        return sip_host
    public = (cfg.get("public_url") or "").strip()
    if public:
        host = urllib.parse.urlparse(public).hostname
        if host:
            return host
    return ""


async def _resolve_mobile_sip_password(
    extension: str,
    *,
    cfg: dict[str, Any],
    miko_rest: MikoRestClient,
    rest_enabled: bool,
) -> tuple[str, str]:
    """Return (password, auth_id) for native mobile SIP registration."""
    ext = (extension or "").strip()
    pjsip_path = cfg.get("mikopbx_pjsip_conf_path") or ""
    pjsip_container = cfg.get("mikopbx_docker_container") or ""
    cache = int(cfg.get("mikopbx_pjsip_cache_seconds") or 60)

    for auth_id in (ext, f"{ext}-WS"):
        password = ""
        if rest_enabled and miko_rest.enabled:
            try:
                password = (await miko_rest.get_sip_secret(auth_id) or "").strip()
            except MikoRestError:
                password = ""
        if not password:
            password = get_pjsip_peer_secret(
                auth_id,
                path=pjsip_path,
                container=pjsip_container,
                cache_ttl_seconds=cache,
            )
        if password:
            return password, auth_id
    return "", ext


def _kommo_public_config(cfg: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    integration = permissions_db.get_kommo_integration()
    redirect_uri = (integration.get("redirect_uri") or "").strip()
    if not redirect_uri:
        redirect_uri = (cfg.get("mobile_kommo_redirect_uri") or "intermarksoftphone://kommo/oauth").strip()
    return {
        "mode": "client_direct",
        "client_id": (integration.get("client_id") or "").strip(),
        "subdomain": (integration.get("subdomain") or "").strip(),
        "redirect_uri": redirect_uri,
    }


def _feature_flags(cfg: dict[str, Any]) -> dict[str, bool]:
    defaults = {
        "recording_enabled": True,
        "recording_required": True,
        "kommo_required": True,
        "team_directory": True,
    }
    raw = cfg.get("mobile_features") or {}
    if isinstance(raw, dict):
        defaults.update({k: bool(v) for k, v in raw.items() if k in defaults})
    return defaults


async def resolve_miko_status(
    *,
    cfg: dict[str, Any],
    miko_rest: MikoRestClient,
    rest_enabled: bool,
) -> str:
    if rest_enabled and miko_rest.enabled:
        return "ok" if await miko_rest.ping() else "offline"
    config_db = (cfg.get("config_db_path") or "").strip()
    if config_db and Path(config_db).exists():
        return "ok"
    return "offline"


async def build_login_bundle(
    *,
    username: str,
    password: str,
    cfg: dict[str, Any],
    miko_rest: MikoRestClient,
    rest_enabled: bool,
    jwt_expire_days_fn: Callable[[], int],
    request: Request | None = None,
) -> dict[str, Any]:
    user_row: dict[str, Any] | None = None
    extension = ""
    display_name = ""
    role = "user"

    login_name = (username or "").strip()
    if "@" in login_name:
        app_user = authenticate_app_user(login_name, password)
        if app_user is not None:
            extension = (app_user.get("extension") or "").strip()
            display_name = app_user.get("email") or extension
            role = "user"
            user_row = app_user
    else:
        mikopbx_user = authenticate_mikopbx_user(
            login_name,
            password,
            cfg["config_db_path"],
        )
        if mikopbx_user is not None:
            extension = (mikopbx_user.get("extension") or "").strip()
            display_name = (mikopbx_user.get("name") or extension).strip()
            role = "user"
            user_row = mikopbx_user

    if user_row is None or not extension:
        raise HTTPException(401, "Invalid username or password")

    token = create_jwt(
        extension,
        cfg["jwt_secret"],
        jwt_expire_days_fn(),
        role=role,
        name=display_name if display_name != extension else None,
        extension=extension,
    )

    sip_host = _resolve_sip_host(cfg)
    sip_password, auth_id = await _resolve_mobile_sip_password(
        extension,
        cfg=cfg,
        miko_rest=miko_rest,
        rest_enabled=rest_enabled,
    )
    transport = (cfg.get("mobile_sip_transport") or "tls").strip().lower()
    if transport not in ("udp", "tcp", "tls"):
        transport = "tls"

    proxy = f"sip:{sip_host};transport={transport}" if sip_host else ""
    expires_in = _jwt_expire_seconds(jwt_expire_days_fn)

    return {
        "access_token": token,
        "refresh_token": token,
        "expires_in": expires_in,
        "user": {
            "id": extension,
            "display_name": display_name or extension,
            "extension": extension,
            "role": role,
        },
        "sip": {
            "domain": sip_host,
            "proxy": proxy,
            "username": extension,
            "auth_id": auth_id,
            "password": sip_password,
            "transport": transport,
            "stun": None,
            "turn": None,
            "enable_srtp": transport == "tls",
        },
        "features": _feature_flags(cfg),
        "kommo": _kommo_public_config(cfg, request),
    }


def register_mobile_v1_routes(
    app,
    *,
    cfg: dict[str, Any],
    miko_rest: MikoRestClient,
    rest_enabled: Callable[[], bool],
    jwt_expire_days: Callable[[], int],
) -> None:
    """Attach /v1/* routes to the main FastAPI app."""

    @router.get("/health")
    async def mobile_health():
        miko = await resolve_miko_status(
            cfg=cfg,
            miko_rest=miko_rest,
            rest_enabled=rest_enabled(),
        )
        return {"backend": "ok", "miko": miko}

    @router.post("/auth/login")
    async def mobile_login(body: MobileLoginRequest, request: Request):
        return await build_login_bundle(
            username=body.username,
            password=body.password,
            cfg=cfg,
            miko_rest=miko_rest,
            rest_enabled=rest_enabled(),
            jwt_expire_days_fn=jwt_expire_days,
            request=request,
        )

    @router.post("/auth/refresh")
    async def mobile_refresh(body: MobileRefreshRequest):
        token = (body.refresh_token or "").strip()
        if not token:
            raise HTTPException(400, "refresh_token is required")
        payload = decode_jwt(token, cfg["jwt_secret"])
        if payload is None:
            raise HTTPException(401, "Invalid or expired token")

        extension = (payload.get("ext") or payload.get("sub") or "").strip()
        if not extension:
            raise HTTPException(401, "Invalid token payload")

        new_token = create_jwt(
            extension,
            cfg["jwt_secret"],
            jwt_expire_days(),
            role=payload.get("role") or "user",
            name=payload.get("name"),
            extension=extension,
        )
        return {
            "access_token": new_token,
            "refresh_token": new_token,
            "expires_in": _jwt_expire_seconds(jwt_expire_days),
        }

    app.include_router(router)
