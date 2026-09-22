"""
FastAPI mount: same-origin web softphone API + static SPA (replaces softphone-bff).

Internal calls use httpx.ASGITransport against the same FastAPI app — no TCP loopback,
no duplicated auth/CDR logic.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("gateway_web_softphone")

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
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


def _strip_cookie_persistence(set_cookie: bytes) -> bytes:
    """Remove Max-Age/Expires so the browser keeps a session cookie only."""
    text = set_cookie.decode("latin-1")
    parts = text.split(";")
    kept = [parts[0]]
    for part in parts[1:]:
        name = part.strip().split("=", 1)[0].lower()
        if name in ("max-age", "expires"):
            continue
        kept.append(part)
    return ";".join(kept).encode("latin-1")


class PublicComputerSessionMiddleware(BaseHTTPMiddleware):
    """When ``public_computer`` is set in session, drop persistent cookie attributes."""

    def __init__(self, app: ASGIApp, *, session_cookie_name: str):
        super().__init__(app)
        self.session_cookie_name = session_cookie_name

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.session.get("public_computer"):
            return response
        prefix = f"{self.session_cookie_name}=".encode("latin-1")
        raw = list(response.raw_headers)
        response.raw_headers = [
            (name, _strip_cookie_persistence(value) if name.lower() == b"set-cookie" and value.startswith(prefix) else value)
            for name, value in raw
        ]
        return response


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


def _asgi_transport(asgi_app: ASGIApp) -> httpx.ASGITransport:
    """httpx >= 0.27 supports ``lifespan='off'``; older versions do not."""
    try:
        return httpx.ASGITransport(app=asgi_app, lifespan="off")
    except TypeError:
        return httpx.ASGITransport(app=asgi_app)


async def _asgi_fetch(
    request: Request,
    method: str,
    path: str,
    *,
    extra_headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Dispatch a sub-request to the same FastAPI/Starlette app (in-process).

    Uses the ASGI stack captured **before** SessionMiddleware so nested calls do
    not re-enter session / BaseHTTPMiddleware (that pattern returns 500s).
    """
    asgi_app: ASGIApp = getattr(request.app.state, "_callspire_internal_asgi", None) or request.app
    transport = _asgi_transport(asgi_app)
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

    # Sub-requests via httpx.ASGITransport must bypass session middleware below.
    app.state._callspire_internal_asgi = app.middleware_stack

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        session_cookie=cfg.session_cookie_name,
        max_age=cfg.session_max_age,
        same_site="lax",
        https_only=https_only,
    )
    app.add_middleware(
        PublicComputerSessionMiddleware,
        session_cookie_name=cfg.session_cookie_name,
    )

    base = cfg.softphone_base.rstrip("/")
    # Browser session API lives under /softphone/api — separate from desktop JWT routes at /api/*.
    api = f"{base}/api"
    router = APIRouter(tags=["web-softphone"])

    def require_session(request: Request) -> None:
        if not request.session.get("proxyJwt") or not request.session.get("me"):
            raise HTTPException(status_code=401, detail="Not authenticated")

    async def _proxy_headers(request: Request) -> dict[str, str]:
        extra: dict[str, str] = {}
        tok = request.session.get("proxyJwt")
        if tok:
            extra["Authorization"] = f"Bearer {tok}"
        if cfg.service_token:
            extra["X-Callspire-Service-Token"] = cfg.service_token
        return extra

    async def proxy_json(request: Request, method: str, path: str, body: Any | None = None) -> Any:
        try:
            extra = await _proxy_headers(request)
            content: bytes | None = None
            if body is not None:
                raw = json.dumps(body).encode()
                extra.setdefault("Content-Type", "application/json")
                content = raw
            r = await _asgi_fetch(request, method, path, extra_headers=extra, content=content)
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("proxy_json %s %s failed", method, path)
            raise HTTPException(502, f"Gateway proxy error: {exc!s}") from exc
        if r.status_code >= 400:
            detail = _json_or_text(r)
            if isinstance(detail, str):
                msg = detail
            elif isinstance(detail, dict):
                msg = detail.get("detail", detail.get("message", r.reason_phrase))
                if isinstance(msg, list):
                    msg = "; ".join(str(x) for x in msg)
                elif not isinstance(msg, str):
                    msg = str(msg)
            else:
                msg = r.reason_phrase
            log.warning("proxy_json %s %s -> %s: %s", method, path, r.status_code, msg)
            raise HTTPException(status_code=r.status_code, detail=msg)
        return _json_or_text(r)

    async def proxy_stream(request: Request, path: str) -> StreamingResponse:
        extra = await _proxy_headers(request)
        r = await _asgi_fetch(request, "GET", path, extra_headers=extra)
        if r.status_code >= 400:
            detail = _json_or_text(r)
            msg = detail if isinstance(detail, str) else detail.get("detail", r.reason_phrase)
            raise HTTPException(status_code=r.status_code, detail=msg)
        headers: dict[str, str] = {}
        for key in ("content-disposition", "content-length"):
            if r.headers.get(key):
                headers[key] = r.headers[key]
        media_type = r.headers.get("content-type") or "application/octet-stream"

        async def body():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                await r.aclose()

        return StreamingResponse(body(), media_type=media_type, headers=headers)

    @router.post(f"{api}/auth/login")
    async def browser_login(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, detail="Invalid JSON")
        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        public_computer = bool(payload.get("public_computer"))
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
        request.session["public_computer"] = public_computer
        me = {
            "email": email,
            "extension": data.get("extension") or "",
            "must_change_password": bool(data.get("must_change_password")),
            "public_computer": public_computer,
        }
        request.session["me"] = me
        return {"ok": True, "me": me, "public_computer": public_computer}

    @router.post(f"{api}/auth/logout")
    async def browser_logout(request: Request):
        request.session.clear()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(cfg.session_cookie_name, path="/")
        return resp

    @router.get(f"{api}/me")
    async def browser_me(request: Request):
        require_session(request)
        me = dict(request.session["me"])
        me["public_computer"] = bool(request.session.get("public_computer"))
        try:
            fresh = await proxy_json(request, "GET", "/api/v1/me")
            if isinstance(fresh, dict):
                if fresh.get("extension"):
                    me["extension"] = str(fresh["extension"])
                if "must_change_password" in fresh:
                    me["must_change_password"] = bool(fresh["must_change_password"])
                request.session["me"] = me
        except HTTPException:
            pass
        return me

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
        result = await proxy_json(
            request,
            "POST",
            "/api/v1/me/change-password",
            {"current_password": cur, "new_password": newp},
        )
        me = dict(request.session.get("me") or {})
        me["must_change_password"] = False
        if isinstance(result, dict):
            if result.get("token"):
                request.session["proxyJwt"] = result["token"]
            if result.get("extension"):
                me["extension"] = str(result["extension"])
            if "must_change_password" in result:
                me["must_change_password"] = bool(result["must_change_password"])
        request.session["me"] = me
        return result

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
        ws_url = cfg.webrtc_sip_ws_url
        sip_host = cfg.webrtc_sip_host
        me = request.session.get("me") or {}
        ext_session = str(me.get("extension") or "").strip()
        extension = ext_session
        sip_password = ""
        sip_auth_user = ""
        sip_contact_user_part = ""
        ice_servers: list[dict[str, Any]] = []
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
                raw_ice = r.get("iceServers")
                if isinstance(raw_ice, list):
                    ice_servers = raw_ice
        except HTTPException as exc:
            # Same as BFF: leave ws_url empty on 403 / failure
            log.warning("webrtc config proxy: %s", exc.detail)
        except Exception as exc:
            log.exception("webrtc config proxy failed")
            # Never fail the whole page load because WSS lookup failed
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

    @router.get(f"{api}/recording")
    async def recording(request: Request):
        require_session(request)
        q = request.url.query
        if not q or "linkedid=" not in q:
            raise HTTPException(400, detail="linkedid query parameter is required")
        path = f"/api/recording?{q}"
        return await proxy_stream(request, path)

    @router.get(f"{api}/kommo/status")
    async def kommo_status(request: Request):
        require_session(request)
        return await proxy_json(request, "GET", "/api/kommo/status")

    @router.get(f"{api}/kommo/session")
    async def kommo_session(request: Request, force_refresh: bool = Query(False)):
        require_session(request)
        path = "/api/kommo/session"
        if force_refresh:
            path += "?force_refresh=true"
        return await proxy_json(request, "GET", path)

    @router.post(f"{api}/kommo/process-call")
    async def kommo_process_call(request: Request):
        require_session(request)
        body = await request.json()
        return await proxy_json(request, "POST", "/api/kommo/process-call", body=body)

    @router.get(f"{api}/kommo/process-call/{{job_id}}")
    async def kommo_process_call_status(request: Request, job_id: str):
        require_session(request)
        return await proxy_json(request, "GET", f"/api/kommo/process-call/{job_id}")

    @router.get(f"{api}/kommo/contact")
    async def kommo_contact(request: Request):
        require_session(request)
        q = request.url.query
        path = f"/api/kommo/contact?{q}" if q else "/api/kommo/contact"
        return await proxy_json(request, "GET", path)

    @router.get(f"{api}/kommo/call-attachments")
    async def kommo_call_attachments(request: Request, hours: int = Query(72)):
        require_session(request)
        path = f"/api/kommo/call-attachments?hours={hours}"
        return await proxy_json(request, "GET", path)

    @router.put(f"{api}/kommo/process-call/{{job_id}}/recording")
    async def kommo_process_call_recording(request: Request, job_id: str):
        require_session(request)
        extra = await _proxy_headers(request)
        content = await request.body()
        ct = request.headers.get("content-type")
        if ct:
            extra["Content-Type"] = ct
        r = await _asgi_fetch(
            request,
            "PUT",
            f"/api/kommo/process-call/{job_id}/recording",
            extra_headers=extra,
            content=content,
        )
        if r.status_code >= 400:
            detail = _json_or_text(r)
            msg = detail if isinstance(detail, str) else detail.get("detail", r.reason_phrase)
            raise HTTPException(status_code=r.status_code, detail=msg)
        return _json_or_text(r)

    @router.post(f"{api}/kommo/process-call/retry")
    async def kommo_process_call_retry(request: Request):
        require_session(request)
        body = await request.json()
        return await proxy_json(request, "POST", "/api/kommo/process-call/retry", body=body)

    @router.get(f"{api}/sip-auth-failures")
    async def sip_auth_failures(request: Request):
        require_session(request)
        q = request.url.query
        path = f"/api/sip-auth-failures?{q}" if q else "/api/sip-auth-failures"
        return await proxy_json(request, "GET", path)

    @router.get(f"{api}/health")
    async def health():
        return {"ok": True}

    app.include_router(router)

    # Legacy browser path: /api/webrtc/config (older SPA). Desktop JWT APIs stay at /api/* on app.py.
    legacy_api = (cfg.api_prefix or "/api").rstrip("/")
    legacy_webrtc = f"{legacy_api}/webrtc/config"
    namespaced_webrtc = f"{api}/webrtc/config"
    if legacy_webrtc != namespaced_webrtc:
        webrtc_ep = next(
            (r.endpoint for r in router.routes if getattr(r, "path", "") == namespaced_webrtc),
            None,
        )
        if webrtc_ep is not None:
            app.add_api_route(legacy_webrtc, webrtc_ep, methods=["GET"], tags=["web-softphone-legacy"])

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
        return _static_file_response(index)

    def _static_file_response(path: Path) -> FileResponse:
        """Serve a built asset with correct cache directives for Vite output.

        - ``index.html``: ``no-cache, no-store`` — browsers must always fetch the
          latest shell; operators get UI updates immediately after a redeploy.
        - ``assets/*.js`` / ``assets/*.css``: ``immutable`` — Vite content-hashes
          every bundle filename; a new deploy produces a new URL, so caching
          forever is safe.
        - Everything else (favicon, robots.txt …): short-lived ``max-age=3600``
          so non-hashed files are refreshed hourly.
        """
        resp = FileResponse(path)
        name = path.name
        if name == "index.html":
            resp.headers["Cache-Control"] = "no-cache, no-store"
        elif "/assets/" in path.as_posix():
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    @app.get(f"{base}/{{full_path:path}}")
    async def softphone_spa(full_path: str):
        """Serve static files under /softphone; unknown paths -> index.html (SPA)."""
        # Never serve index.html for /softphone/api/* — that breaks login with "HTML instead of JSON".
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "API route not found - redeploy gateway_web_softphone/mount.py")
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
                return _static_file_response(index)
            if candidate.is_file():
                return _static_file_response(candidate)
        return _static_file_response(index)
