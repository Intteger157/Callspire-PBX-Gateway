"""
FastAPI mount: same-origin web softphone API + static SPA (replaces softphone-bff).

Internal calls use httpx.ASGITransport against the same FastAPI app — no TCP loopback,
no duplicated auth/CDR logic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp

# Hop-by-hop / client session — must not be forwarded to internal ASGI call
_HOP = frozenset(
    {
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _session_secure_flag() -> bool:
    """Match softphone-bff: set SESSION_SECURE=true behind HTTPS; default false for local HTTP."""
    raw = (_env("SESSION_SECURE") or "").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    return False


@dataclass
class WebSoftphoneConfig:
    """Paths and secrets for the browser UI mount."""

    static_dir: Path
    """Directory with built SPA (index.html, assets/)."""

    session_secret: str
    """Same role as softphone-bff SESSION_SECRET — signs the session cookie."""

    session_cookie_name: str = "callspire_softphone_sid"
    session_max_age: int = 60 * 60 * 24 * 7  # 7 days

    softphone_base: str = "/softphone"
    api_prefix: str = "/api"

    trust_proxy: bool = False
    """If True, app should use ProxyHeadersMiddleware / uvicorn --proxy-headers (recommended)."""

    service_token: str = ""
    """Optional X-Callspire-Service-Token for gateway endpoints that require it."""

    # Fallback WebRTC (when gateway DB / admin does not set wsUrl/sipHost)
    webrtc_sip_ws_url: str = ""
    webrtc_sip_host: str = ""
    webrtc_turn_urls: str = ""
    webrtc_turn_username: str = ""
    webrtc_turn_password: str = ""

    mount_root_redirect: bool = True
    """Если False — не регистрировать ``GET /`` (редирект на ``/softphone/``), когда маршрут ``/`` уже занят."""

    @classmethod
    def from_env(cls, static_dir: Path | None = None) -> WebSoftphoneConfig:
        env_dir = _env("SOFTPHONE_STATIC_DIR").strip()
        if static_dir is not None:
            root = static_dir
        elif env_dir:
            root = Path(env_dir).expanduser()
        else:
            root = Path.cwd() / "softphone-web" / "dist"
        sec = _env("SESSION_SECRET")
        skip_root = _env_bool("WEB_SOFTPHONE_SKIP_ROOT_REDIRECT", False)
        return cls(
            static_dir=root.resolve(),
            session_secret=sec or "dev-secret-change-me",
            session_cookie_name=_env("SESSION_NAME", "callspire_softphone_sid"),
            trust_proxy=_env_bool("TRUST_PROXY"),
            service_token=_env("PBX_GATEWAY_SERVICE_TOKEN", _env("CDR_PROXY_SERVICE_TOKEN", "")),
            webrtc_sip_ws_url=_env("WEBRTC_SIP_WS_URL"),
            webrtc_sip_host=_env("WEBRTC_SIP_HOST"),
            webrtc_turn_urls=_env("WEBRTC_TURN_URLS"),
            webrtc_turn_username=_env("WEBRTC_TURN_USERNAME"),
            webrtc_turn_password=_env("WEBRTC_TURN_PASSWORD"),
            mount_root_redirect=not skip_root,
        )


async def _asgi_fetch(
    request: Request,
    method: str,
    path: str,
    *,
    extra_headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Dispatch a sub-request to the same FastAPI/Starlette app (in-process)."""
    app: ASGIApp = request.app
    transport = httpx.ASGITransport(app=app, lifespan="off")
    headers: list[tuple[str, str]] = []
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in _HOP or kl == "authorization":
            continue
        headers.append((k, v))
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append((k, v))
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi.internal") as client:
        return await client.request(
            method.upper(),
            path,
            headers=headers,
            content=content,
        )


def _json_or_text(r: httpx.Response) -> Any:
    ct = r.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return r.json()
        except json.JSONDecodeError:
            return {}
    return r.text


def mount_web_softphone(app, config: WebSoftphoneConfig | None = None) -> None:
    """
    Register session middleware, browser API routes, and static SPA on ``app``.

    Call **after** all gateway routes (``/api/v1/...``, ``/api/...``) are registered
    on the same FastAPI instance so in-process ASGI sub-requests hit them.

    **Middleware order:** call **after** other ``add_middleware`` calls so
    ``SessionMiddleware`` is outermost (same idea as starting BFF last behind nginx).
    """
    if getattr(app.state, "_callspire_web_softphone_mounted", False):
        raise RuntimeError("mount_web_softphone() was already called on this app")
    app.state._callspire_web_softphone_mounted = True

    cfg = config or WebSoftphoneConfig.from_env()
    if not cfg.session_secret or cfg.session_secret == "dev-secret-change-me":
        if _env("NODE_ENV") == "production" or os.environ.get("CALLSPIRE_ENV") == "production":
            import warnings

            warnings.warn(
                "SESSION_SECRET is missing or default — set a strong secret in production.",
                stacklevel=2,
            )

    https_only = _session_secure_flag()

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        session_cookie=cfg.session_cookie_name,
        max_age=cfg.session_max_age,
        same_site="lax",
        https_only=https_only,
    )

    router = APIRouter(tags=["web-softphone"])
    base = cfg.softphone_base.rstrip("/")
    api = cfg.api_prefix.rstrip("/")

    def require_session(request: Request) -> None:
        if not request.session.get("proxyJwt") or not request.session.get("me"):
            raise HTTPException(status_code=401, detail="Not authenticated")

    async def proxy_json(request: Request, method: str, path: str, body: Any | None = None) -> Any:
        extra: dict[str, str] = {}
        tok = request.session.get("proxyJwt")
        if tok:
            extra["Authorization"] = f"Bearer {tok}"
        if cfg.service_token:
            extra["X-Callspire-Service-Token"] = cfg.service_token
        content: bytes | None = None
        if body is not None:
            raw = json.dumps(body).encode()
            extra.setdefault("Content-Type", "application/json")
            content = raw
        r = await _asgi_fetch(request, method, path, extra_headers=extra, content=content)
        if r.status_code >= 400:
            detail = _json_or_text(r)
            if isinstance(detail, str):
                msg = detail
            elif isinstance(detail, dict):
                msg = detail.get("detail", detail.get("message", r.reason_phrase))
                if isinstance(msg, list):
                    msg = "; ".join(str(x) for x in msg)
            else:
                msg = r.reason_phrase
            raise HTTPException(status_code=r.status_code, detail=msg)
        return _json_or_text(r)

    @router.post(f"{api}/auth/login")
    async def browser_login(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, detail="Invalid JSON")
        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        if not email or not password:
            raise HTTPException(400, detail="email and password are required")

        body = json.dumps({"username": email, "password": password}).encode()
        extra = {"Content-Type": "application/json"}
        if cfg.service_token:
            extra["X-Callspire-Service-Token"] = cfg.service_token
        r = await _asgi_fetch(
            request,
            "POST",
            "/api/v1/auth/login",
            extra_headers=extra,
            content=body,
        )
        data = _json_or_text(r)
        if r.status_code >= 400:
            msg = data if isinstance(data, str) else data.get("detail", data.get("message", "Login failed"))
            raise HTTPException(status_code=r.status_code, detail=msg)
        if not isinstance(data, dict) or not data.get("token"):
            raise HTTPException(502, detail="Proxy login did not return token")

        request.session.clear()
        request.session["proxyJwt"] = data["token"]
        request.session["me"] = {
            "email": email,
            "extension": data.get("extension") or "",
            "must_change_password": bool(data.get("must_change_password")),
        }
        return {"ok": True, "me": request.session["me"]}

    @router.post(f"{api}/auth/logout")
    async def browser_logout(request: Request):
        request.session.clear()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(cfg.session_cookie_name, path="/")
        return resp

    @router.get(f"{api}/me")
    async def browser_me(request: Request):
        require_session(request)
        return request.session["me"]

    @router.post(f"{api}/me/change-password")
    async def browser_change_password(request: Request):
        require_session(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, detail="Invalid JSON")
        cur = str(payload.get("current_password") or "")
        newp = str(payload.get("new_password") or "")
        if not cur or not newp:
            raise HTTPException(400, detail="current_password and new_password are required")
        r = await proxy_json(
            request,
            "POST",
            "/api/v1/me/change-password",
            {"current_password": cur, "new_password": newp},
        )
        me = request.session.get("me")
        if isinstance(me, dict):
            me["must_change_password"] = False
        return r

    @router.get(f"{api}/me/preferences")
    async def get_prefs(request: Request):
        require_session(request)
        return await proxy_json(request, "GET", "/api/v1/me/preferences")

    @router.put(f"{api}/me/preferences")
    async def put_prefs(request: Request):
        require_session(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        return await proxy_json(request, "PUT", "/api/v1/me/preferences", body)

    @router.get(f"{api}/webrtc/config")
    async def webrtc_config(request: Request):
        require_session(request)
        ice_servers: list[dict[str, Any]] = [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun1.l.google.com:19302"},
        ]
        if cfg.webrtc_turn_urls:
            for u in [x.strip() for x in cfg.webrtc_turn_urls.split(",") if x.strip()]:
                entry: dict[str, Any] = {"urls": u}
                if cfg.webrtc_turn_username:
                    entry["username"] = cfg.webrtc_turn_username
                if cfg.webrtc_turn_password:
                    entry["credential"] = cfg.webrtc_turn_password
                ice_servers.append(entry)

        ws_url = cfg.webrtc_sip_ws_url
        sip_host = cfg.webrtc_sip_host
        me = request.session.get("me") or {}
        ext_session = str(me.get("extension") or "").strip()
        extension = ext_session
        sip_password = ""
        sip_auth_user = ""
        sip_contact_user_part = ""
        try:
            r = await proxy_json(request, "GET", "/api/v1/webrtc/config")
            if isinstance(r, dict):
                w = str(r.get("wsUrl") or "").strip()
                h = str(r.get("sipHost") or "").strip()
                if w:
                    ws_url = w
                if h:
                    sip_host = h
                ex = str(r.get("extension") or "").strip()
                if ex:
                    extension = ex
                sp = str(r.get("sipPassword") or "").strip()
                if sp:
                    sip_password = sp
                au = str(r.get("sipAuthUser") or "").strip()
                if au:
                    sip_auth_user = au
                sc = str(r.get("sipContactUserPart") or "").strip()
                if sc:
                    sip_contact_user_part = sc
        except HTTPException:
            # Same as BFF: leave ws_url empty on 403 / failure
            pass

        out: dict[str, Any] = {
            "wsUrl": ws_url,
            "sipHost": sip_host,
            "extension": extension,
            "iceServers": ice_servers,
        }
        if sip_password:
            out["sipPassword"] = sip_password
        if sip_auth_user:
            out["sipAuthUser"] = sip_auth_user
        if sip_contact_user_part:
            out["sipContactUserPart"] = sip_contact_user_part
        return out

    @router.get(f"{api}/my-callerids")
    async def my_callerids(request: Request):
        require_session(request)
        return await proxy_json(request, "GET", "/api/my-callerids")

    @router.post(f"{api}/originate")
    async def originate(request: Request):
        require_session(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, detail="Invalid JSON")
        dest = str(payload.get("destination") or "").strip()
        cid = str(payload.get("callerid") or "").strip()
        if not dest or not cid:
            raise HTTPException(400, detail="destination and callerid are required")
        return await proxy_json(request, "POST", "/api/originate", {"destination": dest, "callerid": cid})

    @router.get(f"{api}/cdr")
    async def cdr(request: Request):
        require_session(request)
        q = request.url.query
        path = f"/api/cdr?{q}" if q else "/api/cdr"
        return await proxy_json(request, "GET", path)

    @router.get(f"{api}/health")
    async def health():
        return {"ok": True}

    app.include_router(router)

    static = cfg.static_dir
    index = static / "index.html"

    if cfg.mount_root_redirect:

        @app.get("/")
        async def root_redirect():
            return RedirectResponse(url=f"{base}/", status_code=302)

    @app.get(base)
    @app.get(f"{base}/")
    async def softphone_index():
        if not index.is_file():
            return JSONResponse(
                {"error": "softphone UI not built", "expected": str(index)},
                status_code=503,
            )
        return FileResponse(index)

    @app.get(f"{base}/{{full_path:path}}")
    async def softphone_spa(full_path: str):
        """Serve static files under /softphone; unknown paths -> index.html (SPA)."""
        if not index.is_file():
            return JSONResponse(
                {"error": "softphone UI not built", "expected": str(index)},
                status_code=503,
            )
        root = static.resolve()
        if full_path:
            candidate = (static / full_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return FileResponse(index)
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index)
