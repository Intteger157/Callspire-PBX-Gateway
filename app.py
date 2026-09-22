"""Callspire PBX Gateway — JWT API for Callspire clients next to MikoPBX.

Runs on or near MikoPBX and exposes CDR, recordings, originate, WebRTC/SIP
settings for the browser softphone, admin tools, and more. Legacy mode reads
CDR from SQLite and resolves outbound CallerID via trunk config; REST mode
delegates to MikoPBX API v3.

Clients authenticate via JWT (browser login, app login, or winapp flow).

Admin users (from config.yaml) manage CallerID permissions, SIP peers, AMI,
and app users via ``/admin``.
"""

import os
import re
import shutil
import sqlite3
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import hmac
from typing import Callable, Optional

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import (
    authenticate_user,
    authenticate_mikopbx_user,
    authenticate_app_user,
    create_jwt,
    decode_jwt,
)
from cdr_client import (
    query_cdr,
    find_recording_path,
    linkedid_involves_extension,
    query_cdr_rest,
    linkedid_involves_extension_rest,
    find_cdr_by_linkedid_rest,
    load_trunk_callerids_rest,
    aggregate_cdr_calls,
    pbx_local_cdr_window,
    normalize_cdr_query_window,
)
from config import load_config
import permissions_db
from pjsip_secrets import get_peer_secret as get_pjsip_peer_secret
from ami_client import ami_originate
from connection_check import check_ami_tcp, check_wss_tls
from miko_rest_client import MikoRestClient, MikoRestConfig, MikoRestError
from app_kommo import register_kommo_routes
import kommo_service

try:
    from gateway_web_softphone import install_web_softphone as _install_web_softphone
except ImportError:
    _install_web_softphone = None

cfg = load_config()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
security = HTTPBearer(auto_error=False)


def _jwt_expire_days() -> int:
    """Current JWT lifetime from config. ``0`` = never expire."""
    val = cfg.get("jwt_expire_days")
    if val is None:
        return 30
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 30


def _build_rest_client() -> MikoRestClient:
    return MikoRestClient(MikoRestConfig(
        base_url=(cfg.get("mikopbx_rest_url") or "").strip(),
        api_key=(cfg.get("mikopbx_api_key") or "").strip(),
        login=(cfg.get("mikopbx_admin_login") or "").strip(),
        password=(cfg.get("mikopbx_admin_password") or ""),
        verify_ssl=bool(cfg.get("mikopbx_verify_ssl", True)),
        timeout_seconds=float(cfg.get("mikopbx_rest_timeout_seconds") or 15),
    ))


miko_rest = _build_rest_client()


def _rest_enabled() -> bool:
    """Central switch: is REST active for this request?

    We require both the master toggle AND the client to have enough config to
    make real calls. That way ``use_rest_api: true`` without credentials
    becomes a soft-disable instead of a crash loop.
    """
    return bool(cfg.get("use_rest_api")) and miko_rest.enabled


async def resolve_webrtc_sip_password_and_auth_user(extension: str) -> tuple[str, str]:
    """Resolve SIP password and digest username for browser WebRTC REGISTER.

    MikoPBX often uses a separate PJSIP auth id ``<ext>-WS`` for the WSS
    transport; the plaintext secret may not match the desk-phone ``<ext>``
    line in ``[ <ext>-AUTH ]``. The browser must send the password that
    belongs to the same auth object Asterisk challenges for. We try
    ``<ext>-WS`` first, then the base extension (Miko REST, then pjsip.conf).
    """
    e = (extension or "").strip()
    if not e:
        return "", ""
    if e.upper().endswith("-WS"):
        base = e[:-3]
        webrtc_id = e
    else:
        base = e
        webrtc_id = f"{e}-WS"
    pjsip_path = cfg.get("mikopbx_pjsip_conf_path") or ""
    pjsip_container = cfg.get("mikopbx_docker_container") or ""
    cache = int(cfg.get("mikopbx_pjsip_cache_seconds") or 60)
    for auth_id in (webrtc_id, base):
        pw = ""
        if _rest_enabled():
            try:
                pw = (await miko_rest.get_sip_secret(auth_id) or "").strip()
            except MikoRestError as exc:
                print(
                    f"[webrtc/config] miko_rest.get_sip_secret failed for {auth_id!r}: {exc}",
                    flush=True,
                )
        if not pw:
            pw = get_pjsip_peer_secret(
                auth_id,
                path=pjsip_path,
                container=pjsip_container,
                cache_ttl_seconds=cache,
            )
        if pw:
            return pw, auth_id
    return "", base


def _cdr_docker_kwargs() -> dict:
    return {
        "docker_container": (cfg.get("mikopbx_docker_container") or "").strip() or None,
        "docker_db_path": (cfg.get("cdr_docker_db_path") or "").strip() or None,
    }


def _banner(msg: str) -> None:
    """Emit a startup banner line. ``flush=True`` is required under systemd:
    stdout is line-buffered in TTY mode but block-buffered when uvicorn runs as
    a service, so banner lines otherwise only appear after enough request
    traffic forces a flush.
    """
    print(msg, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    permissions_db.init_db()
    try:
        from config import CONFIG_PATH as _CONFIG_PATH  # type: ignore
    except Exception:
        _CONFIG_PATH = "config.yaml"
    _banner(f"[pbx-gateway] Starting on port {cfg['port']}  (SSL={'yes' if cfg.get('ssl_certfile') else 'no'})")
    _banner(f"[pbx-gateway] Config file: {_CONFIG_PATH}")
    if (cfg.get("cdr_docker_db_path") or "").strip():
        c = (cfg.get("mikopbx_docker_container") or "").strip()
        _banner(f"[pbx-gateway] CDR DB:    docker {c}:{cfg['cdr_docker_db_path'].strip()}")
    else:
        _banner(f"[pbx-gateway] CDR DB:    {cfg['cdr_db_path']}")
    _banner(f"[pbx-gateway] Config DB: {cfg['config_db_path']}")
    try:
        cdr_exists = Path(cfg["cdr_db_path"]).exists()
        cfg_exists = Path(cfg["config_db_path"]).exists()
        _banner(f"[pbx-gateway] CDR exists:   {cdr_exists}")
        _banner(f"[pbx-gateway] Config exists:{cfg_exists}")
    except Exception as _ex:
        _banner(f"[pbx-gateway] Path check failed: {_ex}")

    # REST API status: surface ONCE at startup so a misconfigured PBX shows up
    # immediately in journalctl instead of silently 502-ing on first request.
    if cfg.get("use_rest_api"):
        if not miko_rest.enabled:
            _banner(
                "[pbx-gateway] REST: use_rest_api=True but client is NOT configured "
                "(missing mikopbx_rest_url and/or api_key/admin creds). "
                "Falling back to legacy SQLite path."
            )
        else:
            _banner(f"[pbx-gateway] REST URL:  {cfg.get('mikopbx_rest_url')}")
            ok = await miko_rest.ping()
            _banner(f"[pbx-gateway] REST ping: {'OK' if ok else 'FAILED'}")
    else:
        _banner("[pbx-gateway] REST:      disabled (use_rest_api=False)")

    jwt_days = _jwt_expire_days()
    _banner(
        "[pbx-gateway] JWT sessions: "
        + ("never expire" if jwt_days == 0 else f"{jwt_days} day(s)")
    )

    from app_kommo import ensure_kommo_workers_started

    await ensure_kommo_workers_started()

    try:
        yield
    finally:
        await miko_rest.close()


app = FastAPI(title="Callspire PBX Gateway", lifespan=lifespan)


def _public_url_prefix(request: Request) -> str:
    """External URL path prefix when behind a reverse proxy (e.g. nginx /tool/). Empty when accessed directly.

    Expects ``X-Forwarded-Prefix`` (e.g. ``/tool``) from the proxy — same as your ``proxy_set_header`` line.
    """
    raw = (request.headers.get("x-forwarded-prefix") or "").strip()
    if not raw or raw == "/":
        return ""
    return raw.rstrip("/")


def _html_context(request: Request, **kwargs) -> dict:
    """Context for Jinja templates: ``base_path`` for links (see templates)."""
    ctx: dict = {"base_path": _public_url_prefix(request)}
    ctx.update(kwargs)
    return ctx


def _external_base_url(request: Request) -> str:
    public = (cfg.get("public_url") or "").strip().rstrip("/")
    prefix = _public_url_prefix(request)
    if public:
        if prefix and not public.endswith(prefix):
            return public + prefix
        return public
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        host = f"127.0.0.1:{cfg['port']}"
    return f"{scheme}://{host}{prefix}".rstrip("/")


def _kommo_default_redirect_uri(request: Request) -> str:
    return f"{_external_base_url(request)}/oauth/kommo/callback"


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ======================= Auth dependencies =======================

def require_jwt(creds: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if creds is None:
        raise HTTPException(401, "Missing authorization header")
    payload = decode_jwt(creds.credentials, cfg["jwt_secret"])
    if payload is None:
        raise HTTPException(401, "Invalid or expired token")
    return payload


def require_admin(payload: dict = Depends(require_jwt)) -> dict:
    if payload.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return payload


def _extension_from_token(user: dict) -> str:
    """PBX extension for this token: explicit ext claim, else legacy JWT sub when not an email."""
    ext = (user.get("ext") or "").strip()
    if ext:
        return ext
    sub = (user.get("sub") or "").strip()
    if sub and "@" not in sub:
        return sub
    return ""


def _maybe_require_service_token(*, username: str, x_callspire_service_token: str | None) -> None:
    """Optional extra protection for internet-exposed deployments.

    By default we keep legacy SIP extension+password login working for the Windows softphone.
    If you want to protect email-based app-user login (recommended), set:
      service_token + service_token_mode: "email_only"
    """
    expected = (cfg.get("service_token") or "").strip()
    mode = (cfg.get("service_token_mode") or "off").strip().lower()
    if not expected or mode == "off":
        return

    is_email = "@" in (username or "")
    if mode == "email_only" and not is_email:
        return

    provided = (x_callspire_service_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "Missing or invalid service token")


# ======================= Health =======================

@app.get("/health")
async def health():
    return {"status": "ok"}


# ======================= Login =======================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, callback: str = Query(""), extension: str = Query("")):
    return templates.TemplateResponse(
        request,
        "login.html",
        context=_html_context(
            request,
            callback=callback,
            extension=extension,
            error=None,
        ),
    )


@app.post("/auth/login")
async def auth_login(request: Request, x_callspire_service_token: str | None = Header(default=None)):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    callback = form.get("callback", "")
    extension = form.get("extension", "")

    _maybe_require_service_token(username=username, x_callspire_service_token=x_callspire_service_token)

    token: str | None = None
    # Email: prefer app_users over static admin users (same rationale as /api/v1/auth/login).
    if "@" in username:
        app_user = authenticate_app_user(username, password)
        if app_user is not None:
            token = create_jwt(
                app_user["email"], cfg["jwt_secret"], _jwt_expire_days(),
                role="user",
                extension=app_user.get("extension"),
                must_change_password=app_user.get("must_change_password"),
            )

    if token is None:
        user = authenticate_user(username, password, cfg["users"])
        if user is not None:
            token = create_jwt(
                user["username"], cfg["jwt_secret"], _jwt_expire_days(),
                role="admin",
            )

    if token is None:
        app_user = authenticate_app_user(username, password)
        if app_user is not None:
            token = create_jwt(
                app_user["email"], cfg["jwt_secret"], _jwt_expire_days(),
                role="user",
                extension=app_user.get("extension"),
                must_change_password=app_user.get("must_change_password"),
            )

    if token is None:
        mikopbx_user = authenticate_mikopbx_user(username, password, cfg["config_db_path"])
        if mikopbx_user is not None:
            token = create_jwt(
                mikopbx_user["extension"], cfg["jwt_secret"], _jwt_expire_days(),
                role="user", name=mikopbx_user.get("name"),
            )

    if token is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            context=_html_context(
                request,
                callback=callback,
                extension=extension,
                error="Invalid username or password",
            ),
            status_code=401,
        )

    if callback:
        sep = "&" if "?" in callback else "?"
        redirect_url = f"{callback}{sep}token={urllib.parse.quote(token)}"
        # Show a short success screen, then JS navigates to callspire:// — better UX than an instant 302
        # (user sees confirmation; the desktop app still receives the protocol URL).
        return templates.TemplateResponse(
            request,
            "cdr_auth_success.html",
            context=_html_context(request, redirect_url=redirect_url),
            status_code=200,
        )

    return JSONResponse({"token": token})


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/v1/auth/login")
async def api_login(body: LoginRequest, x_callspire_service_token: str | None = Header(default=None)):
    """JSON login for backend-to-backend usage (e.g. BFF -> proxy).

    Returns a JWT identical to /auth/login (form-based). Protected by optional
    service token to avoid exposing password auth directly to the internet.
    """
    username = (body.username or "").strip()
    password = body.password or ""

    _maybe_require_service_token(username=username, x_callspire_service_token=x_callspire_service_token)

    # Email-shaped logins: prefer app_users (mapped extension) over static admin users in config.
    # Otherwise the same email as an admin login gets an admin JWT and /api/v1/webrtc/config returns 403 —
    # the softphone then sees empty WSS/host and shows "WebRTC not configured".
    if "@" in username:
        app_user = authenticate_app_user(username, password)
        if app_user is not None:
            token = create_jwt(
                app_user["email"], cfg["jwt_secret"], _jwt_expire_days(),
                role="user",
                extension=app_user.get("extension"),
                must_change_password=app_user.get("must_change_password"),
            )
            return {
                "token": token,
                "role": "user",
                "extension": app_user.get("extension"),
                "must_change_password": bool(app_user.get("must_change_password")),
            }

    user = authenticate_user(username, password, cfg["users"])
    if user is not None:
        token = create_jwt(
            user["username"], cfg["jwt_secret"], _jwt_expire_days(),
            role="admin",
        )
        return {"token": token, "role": "admin"}

    app_user = authenticate_app_user(username, password)
    if app_user is not None:
        token = create_jwt(
            app_user["email"], cfg["jwt_secret"], _jwt_expire_days(),
            role="user",
            extension=app_user.get("extension"),
            must_change_password=app_user.get("must_change_password"),
        )
        return {
            "token": token,
            "role": "user",
            "extension": app_user.get("extension"),
            "must_change_password": bool(app_user.get("must_change_password")),
        }

    mikopbx_user = authenticate_mikopbx_user(username, password, cfg["config_db_path"])
    if mikopbx_user is not None:
        token = create_jwt(
            mikopbx_user["extension"], cfg["jwt_secret"], _jwt_expire_days(),
            role="user", name=mikopbx_user.get("name"),
        )
        return {"token": token, "role": "user", "extension": mikopbx_user.get("extension"), "must_change_password": False}

    raise HTTPException(401, "Invalid username or password")


@app.post("/winapp-auth")
async def winapp_auth(body: LoginRequest):
    """Windows softphone login (extension + SIP password).

    Kept as a dedicated endpoint so the Windows client doesn't mix with browser login flows.
    """
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or "@" in username:
        raise HTTPException(400, "username must be a MikoPBX extension")

    mikopbx_user = authenticate_mikopbx_user(username, password, cfg["config_db_path"])
    if mikopbx_user is None:
        raise HTTPException(401, "Invalid username or password")

    token = create_jwt(
        mikopbx_user["extension"], cfg["jwt_secret"], _jwt_expire_days(),
        role="user", name=mikopbx_user.get("name"),
    )
    return {"token": token, "role": "user", "extension": mikopbx_user.get("extension"), "must_change_password": False}


# ======================= CDR =======================

def _filter_cdr_noise(records: list[dict], ext: str | None) -> list[dict]:
    """Drop click-to-call bootstrap legs from the call-history list.

    Originate creates an extra Local leg whose dst_num is the *caller ID*
    (e.g. +74951396143) — it shows up as a phantom NOANSWER "call" to the
    user's own outbound number. Kommo matching is unaffected: it queries CDR
    by destination phone, which never equals a caller ID.
    """
    cid_digits: set[str] = set()
    if ext:
        try:
            for cid in permissions_db.get_allowed_callerids(ext.strip()) or []:
                digits = "".join(ch for ch in str(cid) if ch.isdigit())
                if digits:
                    cid_digits.add(digits)
        except Exception:
            pass
    out = []
    for r in records:
        if (r.get("appname") or "").strip().lower() == "originate":
            continue
        dst_digits = "".join(ch for ch in str(r.get("dst_num") or "") if ch.isdigit())
        if dst_digits and dst_digits in cid_digits:
            continue
        out.append(r)
    return out


@app.get("/api/cdr")
async def get_cdr(
    ext: str | None = Query(None, description="Source extension (admins only; ignored for normal users)"),
    dst: str | None = Query(None, description="Destination number"),
    start_from: str | None = Query(None, alias="from", description="ISO datetime lower bound"),
    start_to: str | None = Query(None, alias="to", description="ISO datetime upper bound"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_jwt),
):
    ext_filter = ext
    if user.get("role") != "admin":
        my_ext = _extension_from_token(user)
        if not my_ext:
            raise HTTPException(
                403,
                "No PBX extension on this account; cannot load CDR.",
            )
        if ext is not None and ext.strip() != my_ext:
            raise HTTPException(403, "Cannot query CDR for another extension.")
        ext_filter = my_ext

    # Same source selection as the dashboard and Kommo workers: SQLite
    # cdr_general first, Miko REST only as a fallback.
    try:
        records, _source = await _query_miko_cdr(
            ext=ext_filter,
            dst=dst,
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=offset,
        )
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST CDR error: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"CDR query error: {exc}")

    return {"result": True, "data": _filter_cdr_noise(records, ext_filter)}


# ======================= Dashboard (admin analytics) =======================

async def _extension_names() -> dict[str, str]:
    """``ext -> display name`` for every SIP extension; {} when unavailable."""
    try:
        if _rest_enabled():
            items = await miko_rest.list_extensions_for_select(type_="SIP")
            out: dict[str, str] = {}
            for i in items:
                ext = str(i.get("value") or "").strip()
                if ext:
                    out[ext] = _strip_ext_display_name(str(i.get("name") or "")) or ext
            return out
        config_db = cfg["config_db_path"]
        if not Path(config_db).exists():
            return {}
        conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT extension, description FROM m_Sip WHERE type='peer' AND disabled='0'"
            ).fetchall()
            return {r["extension"]: (r["description"] or r["extension"]) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


@app.get("/api/admin/cdr-dashboard")
async def cdr_dashboard(
    start_from: str | None = Query(None, alias="from", description="ISO datetime lower bound"),
    start_to: str | None = Query(None, alias="to", description="ISO datetime upper bound"),
    hours: int | None = Query(None, ge=1, le=8760, description="Preset lookback in hours (PBX-local window)"),
    limit: int = Query(5000, ge=1, le=20000, description="Max CDR legs to scan"),
    _admin: dict = Depends(require_admin),
):
    """One record per call for the admin dashboard: who called where, direction,
    answered/talk time, and the device class (web softphone vs SIP endpoint).
    Country resolution happens client-side from the phone prefix."""
    ext_names = await _extension_names()
    offset = float(cfg.get("pbx_utc_offset_hours") or 0)
    if hours is not None:
        start_from, start_to = pbx_local_cdr_window(hours, pbx_utc_offset_hours=offset)
    else:
        start_from, start_to = normalize_cdr_query_window(
            start_from, start_to, pbx_utc_offset_hours=offset
        )

    try:
        records, cdr_source = await _query_miko_cdr(
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=0,
            max_pages=max(1, min(200, limit // 100 + 1)),
        )
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST CDR error: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"CDR query error: {exc}") from exc

    calls = aggregate_cdr_calls(records, set(ext_names))
    return {
        "result": True,
        "calls": calls,
        "extensions": ext_names,
        "legs_scanned": len(records),
        "truncated": len(records) >= limit,
        "cdr_source": cdr_source,
    }


# ======================= Recording =======================

@app.get("/api/recording")
async def get_recording(
    linkedid: str = Query(..., description="CDR linkedid to identify the call"),
    user: dict = Depends(require_jwt),
):
    """Stream the recording file for a given CDR linkedid.

    In REST mode: we look up the matching CDR entry via the MikoPBX API and
    stream the audio back through a signed ``playback_url``. The client never
    sees the MikoPBX token — the proxy is still the only authenticated hop.
    """
    if _rest_enabled():
        if user.get("role") != "admin":
            my_ext = _extension_from_token(user)
            if not my_ext:
                raise HTTPException(403, "No PBX extension on this account.")
            try:
                allowed = await linkedid_involves_extension_rest(miko_rest, linkedid, my_ext)
            except MikoRestError as exc:
                raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc
            if not allowed:
                raise HTTPException(404, "Recording not found")

        try:
            cdr_row = await find_cdr_by_linkedid_rest(miko_rest, linkedid)
        except MikoRestError as exc:
            raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc

        if cdr_row is None or not (cdr_row.get("playback_url") or "").strip():
            raise HTTPException(404, "No recording found for this call")

        playback_url = cdr_row["playback_url"]
        try:
            upstream = await miko_rest.stream_cdr_playback(playback_url)
        except MikoRestError as exc:
            raise HTTPException(exc.status or 502, exc.message) from exc

        media_type = upstream.headers.get("content-type") or "audio/mpeg"
        filename_hint = _recording_filename_hint(cdr_row)
        headers = {"Content-Disposition": f'inline; filename="{filename_hint}"'}
        # Forward Content-Length when present so progressbars in the UI work.
        if "content-length" in upstream.headers:
            headers["Content-Length"] = upstream.headers["content-length"]

        async def _iter():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(_iter(), media_type=media_type, headers=headers)

    # ----- legacy SQLite + filesystem path -----
    if user.get("role") != "admin":
        my_ext = _extension_from_token(user)
        if not my_ext:
            raise HTTPException(403, "No PBX extension on this account.")
        if not linkedid_involves_extension(
            cfg["cdr_db_path"], linkedid, my_ext, **_cdr_docker_kwargs()
        ):
            raise HTTPException(404, "Recording not found")

    try:
        file_path = find_recording_path(
            cdr_db_path=cfg["cdr_db_path"],
            recording_base=cfg.get("recording_base", "/var/spool/mikopbx"),
            linkedid=linkedid,
            **_cdr_docker_kwargs(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Error looking up recording: {exc}")

    if file_path is None:
        raise HTTPException(404, "No recording found for this call")

    path = Path(file_path)
    if not path.is_file():
        raise HTTPException(404, f"Recording file not found on disk: {path.name}")

    media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=path.name,
    )


def _recording_filename_hint(cdr_row: dict) -> str:
    """Construct a sane download filename from a REST CDR item."""
    rec = (cdr_row.get("recordingfile") or "").strip()
    if rec:
        return os.path.basename(rec) or "recording.mp3"
    linkedid = (cdr_row.get("linkedid") or "").strip() or "recording"
    return f"{linkedid}.mp3"


_verify_linkedid_fn: Optional[Callable] = None


def _kommo_cdr_sqlite_available() -> bool:
    """True when Kommo worker can read CDR from local SQLite (preferred over REST)."""
    docker_db = (cfg.get("cdr_docker_db_path") or "").strip()
    if docker_db:
        return bool((cfg.get("mikopbx_docker_container") or "").strip())
    try:
        return Path(cfg.get("cdr_db_path") or "").is_file()
    except OSError:
        return False


def _kommo_query_cdr_sqlite(
    *,
    ext: str | None = None,
    dst: str | None = None,
    linkedid: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    is_incoming: bool = False,
) -> list:
    return query_cdr(
        cdr_db_path=cfg["cdr_db_path"],
        config_db_path=cfg["config_db_path"],
        ext=ext,
        dst=dst,
        linkedid=linkedid,
        start_from=start_from,
        start_to=start_to,
        limit=limit,
        offset=offset,
        is_incoming=is_incoming,
        **_cdr_docker_kwargs(),
    )


async def _kommo_copy_sqlite_recording(
    linkedid: str, dest: Path, *, billsec: int = 0
) -> bool:
    from kommo_recording import wait_recording_file_stable

    try:
        file_path = find_recording_path(
            cdr_db_path=cfg["cdr_db_path"],
            recording_base=cfg.get("recording_base", "/var/spool/mikopbx"),
            linkedid=linkedid,
            **_cdr_docker_kwargs(),
        )
    except Exception:
        return False
    if not file_path:
        return False
    src = Path(file_path)
    if not src.is_file():
        return False
    if not await wait_recording_file_stable(src, billsec=billsec):
        print(
            f"[kommo_recording] source recording still growing linkedid={linkedid} "
            f"path={src.name}",
            flush=True,
        )
        return False
    shutil.copy2(src, dest)
    return dest.is_file() and dest.stat().st_size > 0


async def _kommo_verify_linkedid(linkedid: str, extension: str) -> bool:
    if _verify_linkedid_fn:
        result = _verify_linkedid_fn(linkedid, extension)
        if hasattr(result, "__await__"):
            return bool(await result)
        return bool(result)
    return True


async def _query_miko_cdr(
    *,
    ext: str | None = None,
    dst: str | None = None,
    linkedid: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    is_incoming: bool = False,
    max_pages: int = 5,
) -> tuple[list, str]:
    """Read CDR from MikoPBX — same source selection as Kommo workers.

    Prefer the on-disk ``cdr_general`` SQLite DB (direct Miko CDR) when
    reachable; fall back to Miko REST ``/cdr`` only when SQLite is unavailable.
    REST is paginated and can miss legs, so dashboard/Kommo analytics stay on
    SQLite whenever the gateway can read it (host path or docker exec).
    """
    if _kommo_cdr_sqlite_available():
        rows = _kommo_query_cdr_sqlite(
            ext=ext,
            dst=dst,
            linkedid=linkedid,
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=offset,
            is_incoming=is_incoming,
        )
        return rows, "miko_sqlite"
    if _rest_enabled():
        trunk_cids = await load_trunk_callerids_rest(miko_rest)
        rows = await query_cdr_rest(
            miko_rest,
            trunk_cids,
            ext=ext,
            dst=dst,
            linkedid=linkedid,
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=offset,
            max_pages=max_pages,
            is_incoming=is_incoming,
        )
        return rows, "miko_rest"
    rows = _kommo_query_cdr_sqlite(
        ext=ext,
        dst=dst,
        linkedid=linkedid,
        start_from=start_from,
        start_to=start_to,
        limit=limit,
        offset=offset,
        is_incoming=is_incoming,
    )
    return rows, "miko_sqlite"


async def _kommo_internal_query_cdr(
    *,
    ext: str | None = None,
    dst: str | None = None,
    linkedid: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    is_incoming: bool = False,
) -> list:
    """CDR rows for Kommo worker (no JWT — extension already validated)."""
    rows, _source = await _query_miko_cdr(
        ext=ext,
        dst=dst,
        linkedid=linkedid,
        start_from=start_from,
        start_to=start_to,
        limit=limit,
        offset=offset,
        is_incoming=is_incoming,
        max_pages=5,
    )
    return rows


async def _kommo_internal_download_recording(
    linkedid: str, dest: Path, *, billsec: int = 0
) -> bool:
    """Download Miko recording to dest path for Kommo upload."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _kommo_cdr_sqlite_available():
        if await _kommo_copy_sqlite_recording(linkedid, dest, billsec=billsec):
            return True
    if _rest_enabled():
        try:
            cdr_row = await find_cdr_by_linkedid_rest(miko_rest, linkedid)
        except MikoRestError:
            cdr_row = None
        if cdr_row and (cdr_row.get("playback_url") or "").strip():
            try:
                upstream = await miko_rest.stream_cdr_playback(cdr_row["playback_url"])
            except MikoRestError:
                upstream = None
            if upstream is not None:
                try:
                    with dest.open("wb") as out:
                        async for chunk in upstream.aiter_bytes():
                            out.write(chunk)
                    if dest.is_file() and dest.stat().st_size > 0:
                        return True
                finally:
                    await upstream.aclose()
    return await _kommo_copy_sqlite_recording(linkedid, dest, billsec=billsec)


async def _kommo_linkedid_for_extension(linkedid: str, extension: str) -> bool:
    if _kommo_cdr_sqlite_available():
        return linkedid_involves_extension(
            cdr_db_path=cfg["cdr_db_path"],
            linkedid=linkedid,
            extension=extension,
            **_cdr_docker_kwargs(),
        )
    if _rest_enabled():
        return await linkedid_involves_extension_rest(miko_rest, linkedid, extension)
    return linkedid_involves_extension(
        cdr_db_path=cfg["cdr_db_path"],
        linkedid=linkedid,
        extension=extension,
        **_cdr_docker_kwargs(),
    )


_verify_linkedid_fn = _kommo_linkedid_for_extension


# ======================= CallerID / Originate =======================


def _normalize_originate_number(raw: str) -> str:
    """E.164 for originate; Russian 8XXXXXXXXXX → +7… only when clearly RU (not +852, +81, …)."""
    from phone_normalize import normalize_for_dial

    return normalize_for_dial(raw)


@app.get("/api/my-callerids")
async def get_my_callerids(user: dict = Depends(require_jwt)):
    """Return CallerID numbers with names assigned to the authenticated user."""
    extension = _extension_from_token(user)
    callerids = permissions_db.get_allowed_callerids(extension)
    names = permissions_db.get_all_callerid_names()
    items = []
    for cid in callerids:
        info = names.get(cid, {})
        items.append({"number": cid, "name": info.get("name", "")})
    return {"extension": extension, "callerids": callerids, "callerid_items": items}


class OriginateRequest(BaseModel):
    destination: str
    callerid: str
    ring_extension: str | None = None


@app.post("/api/originate")
async def originate_call(body: OriginateRequest, user: dict = Depends(require_jwt)):
    """Initiate an outbound call via PBX AMI Originate with selected CallerID."""
    extension = _extension_from_token(user)
    if not extension:
        raise HTTPException(400, "No extension in token for this user")

    destination = _normalize_originate_number(body.destination)
    callerid_norm = _normalize_originate_number(body.callerid)
    if not destination:
        raise HTTPException(400, "destination is required")
    if not callerid_norm:
        raise HTTPException(400, "callerid is required")

    allowed = permissions_db.get_allowed_callerids(extension)
    if callerid_norm not in allowed:
        raise HTTPException(
            403,
            f"CallerID '{callerid_norm}' is not permitted for extension {extension}",
        )

    ring_raw = (getattr(body, "ring_extension", None) or "").strip()
    # Web softphone passes "201-WS" to ring WebRTC leg only (not desktop SIP).
    if ring_raw.upper().endswith("-WS"):
        ring_extension = ring_raw
    else:
        ring_extension = ring_raw or extension

    ami_cfg = permissions_db.get_ami_config()
    originate_id = uuid.uuid4().hex[:16]

    if ring_extension != extension:
        print(
            f"[originate] JWT ext={extension} ring_extension={ring_extension} "
            f"dst={destination} callerid={callerid_norm}",
            flush=True,
        )

    result = await ami_originate(
        config=ami_cfg,
        extension=ring_extension,
        destination=destination,
        callerid=callerid_norm,
        originate_id=originate_id,
    )
    if not result["success"]:
        raise HTTPException(502, result.get("error", "Originate failed"))

    return {"success": True, "originate_id": result["originate_id"]}


# ======================= User: password change (app users) =======================

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.get("/api/v1/me")
async def api_me(user: dict = Depends(require_jwt)):
    """Current app user profile (DB is source of truth for must_change_password)."""
    email = (user.get("sub") or "").strip().lower()
    if email and "@" in email:
        u = permissions_db.get_app_user_by_email(email)
        if u is not None and not u.get("disabled"):
            return {
                "email": u["email"],
                "extension": u.get("mikopbx_extension") or "",
                "must_change_password": bool(u.get("must_change_password")),
            }
    return {
        "email": email or "",
        "extension": user.get("ext") or "",
        "must_change_password": bool(user.get("must_change_password")),
    }


@app.post("/api/v1/me/change-password")
async def change_my_password(body: ChangePasswordRequest, user: dict = Depends(require_jwt)):
    if user.get("role") != "user":
        raise HTTPException(403, "User access required")

    email = (user.get("sub") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "This account cannot change password via API")

    u = permissions_db.get_app_user_by_email(email)
    if u is None or u.get("disabled"):
        raise HTTPException(404, "User not found")

    import bcrypt  # local import to keep dependencies minimal
    if not bcrypt.checkpw(body.current_password.encode(), u["password_hash"].encode()):
        raise HTTPException(401, "Invalid current password")

    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")

    new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    permissions_db.set_app_user_password(email, new_hash, must_change_password=False)
    ext = u.get("mikopbx_extension") or user.get("ext") or ""
    token = create_jwt(
        email,
        cfg["jwt_secret"],
        _jwt_expire_days(),
        role="user",
        extension=ext,
        must_change_password=False,
    )
    return {
        "success": True,
        "token": token,
        "extension": ext,
        "must_change_password": False,
    }


# ======================= User: softphone preferences =======================
#
# Stores per-user UI preferences (audio device selections, AEC/NS/AGC toggles,
# ringtone device). Anything not in the permissions_db whitelist is silently
# dropped. No secrets and no PII are accepted; deviceId values are opaque
# Chromium tokens scoped to the user's profile.

class UpdatePreferencesRequest(BaseModel):
    """Partial update for softphone preferences.

    All fields are optional. Unknown keys the client sends are ignored by
    ``permissions_db.set_user_prefs``. ``None`` or empty string clears a
    stored string field; a bool field must be sent explicitly to change.
    """
    micId: str | None = None
    speakerId: str | None = None
    ringtoneDeviceId: str | None = None
    aec: bool | None = None
    ns: bool | None = None
    agc: bool | None = None
    ringtoneEnabled: bool | None = None


@app.get("/api/v1/me/preferences")
async def get_my_preferences(user: dict = Depends(require_jwt)):
    if user.get("role") != "user":
        raise HTTPException(403, "User access required")
    email = (user.get("sub") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Preferences are only available for app users")
    return {"preferences": permissions_db.get_user_prefs(email)}


@app.put("/api/v1/me/preferences")
async def put_my_preferences(
    body: UpdatePreferencesRequest, user: dict = Depends(require_jwt)
):
    if user.get("role") != "user":
        raise HTTPException(403, "User access required")
    email = (user.get("sub") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Preferences are only available for app users")
    patch = body.model_dump(exclude_unset=True)
    try:
        result = permissions_db.set_user_prefs(email, patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"preferences": result}


def _expand_turn_url_variants(turn_urls: str) -> list[str]:
    """When admin sets UDP-only TURN, also offer TCP (symmetric NAT / firewall)."""
    out: list[str] = []
    for url in (u.strip() for u in (turn_urls or "").split(",") if u.strip()):
        if url not in out:
            out.append(url)
        lower = url.lower()
        if "transport=udp" in lower and not any("transport=tcp" in u.lower() for u in out):
            tcp = url
            for old, new in (("transport=UDP", "transport=TCP"), ("transport=udp", "transport=tcp")):
                if old in tcp:
                    tcp = tcp.replace(old, new, 1)
                    break
            if tcp not in out:
                out.append(tcp)
    return out


def _append_turn_ice_entries(
    servers: list[dict], turn_urls: str, turn_user: str, turn_pass: str
) -> None:
    urls = _expand_turn_url_variants(turn_urls)
    if not urls:
        return
    user = (turn_user or "").strip()
    pwd = turn_pass or ""
    for url in urls:
        entry: dict = {"urls": url}
        if user:
            entry["username"] = user
        if pwd:
            entry["credential"] = pwd
        servers.append(entry)


def _build_ice_servers() -> list[dict]:
    """ICE for browser + desktop WebRTC.

    When TURN is configured in admin *Softphone app settings* (same DB row desktop
    syncs from), use relay-only — no public STUN. Matches WebRtcClient/phone.js:
    STUN + forceRelay still wastes gather time and can delay TURN on strict NAT.
    Falls back to Google STUN when TURN is not configured.
    """
    app_turn = permissions_db.get_softphone_app_settings()
    turn_uri = (app_turn.get("turn_uri") or "").strip()
    turn_user = (app_turn.get("turn_username") or "").strip()
    turn_pass = app_turn.get("turn_password") or ""
    if not turn_uri:
        turn_uri = (os.environ.get("WEBRTC_TURN_URLS") or "").strip()
        turn_user = (os.environ.get("WEBRTC_TURN_USERNAME") or turn_user).strip()
        turn_pass = (os.environ.get("WEBRTC_TURN_PASSWORD") or turn_pass).strip()
    if turn_uri:
        servers: list[dict] = []
        _append_turn_ice_entries(servers, turn_uri, turn_user, turn_pass)
        return servers
    return [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]


@app.get("/api/v1/webrtc/config")
async def api_get_webrtc_config(user: dict = Depends(require_jwt)):
    """WebRTC: WSS, SIP host, extension, ``sipPassword``, optional ``sipAuthUser``.

    Resolves the secret with :func:`resolve_webrtc_sip_password_and_auth_user`
    (``<ext>-WS`` first, then ``<ext>`` — REST, then ``[<id>-AUTH]`` in ``pjsip.conf``)
    so the browser can digest as the same PJSIP auth Miko uses for WSS. ``sipAuthUser``
    is the username that matched the returned password (often ``<ext>-WS`` for WebRTC).
    """
    if user.get("role") != "user":
        raise HTTPException(403, "User access required")
    extension = _extension_from_token(user)
    if not extension:
        raise HTTPException(400, "No extension in token")
    pub = permissions_db.get_webrtc_public_config()
    sip_password, sip_auth_user = await resolve_webrtc_sip_password_and_auth_user(extension)
    out: dict = {
        "wsUrl": pub.get("ws_url") or "",
        "sipHost": pub.get("sip_host") or "",
        "extension": extension,
        "sipPassword": sip_password,
        "iceServers": _build_ice_servers(),
    }
    if sip_password:
        out["sipAuthUser"] = sip_auth_user
    return out


@app.get("/api/v1/softphone-app-settings")
async def api_get_softphone_app_settings(user: dict = Depends(require_jwt)):
    """TURN defaults for Callspire desktop (Windows) — pull on startup when PBX Gateway is enabled."""
    if user.get("role") != "user":
        raise HTTPException(403, "User access required")
    return permissions_db.get_softphone_app_settings()


# ======================= Admin: app users (email login) =======================

class AdminCreateAppUserRequest(BaseModel):
    email: str
    password: str
    mikopbx_extension: str


@app.get("/api/admin/app-users")
async def admin_list_app_users(_admin: dict = Depends(require_admin)):
    return {"users": permissions_db.list_app_users()}


@app.post("/api/admin/app-users")
async def admin_create_app_user(body: AdminCreateAppUserRequest, _admin: dict = Depends(require_admin)):
    email = (body.email or "").strip().lower()
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    import bcrypt  # local import to keep dependencies minimal
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        u = permissions_db.create_app_user(
            email=email,
            password_hash=pw_hash,
            mikopbx_extension=(body.mikopbx_extension or "").strip(),
            must_change_password=True,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.IntegrityError:
        raise HTTPException(409, "User already exists")
    return {"success": True, "user": u}


class AdminUpdateAppUserRequest(BaseModel):
    """Update mapping from web login to MikoPBX extension (e.g. after extension change on PBX)."""

    email: str
    mikopbx_extension: str


@app.patch("/api/admin/app-users")
async def admin_update_app_user(body: AdminUpdateAppUserRequest, _admin: dict = Depends(require_admin)):
    email = (body.email or "").strip().lower()
    ext = (body.mikopbx_extension or "").strip()
    if not ext:
        raise HTTPException(400, "mikopbx_extension is required")
    try:
        ok = permissions_db.update_app_user_mikopbx_extension(email, ext)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "User not found")
    u = permissions_db.get_app_user_by_email(email)
    if u and "password_hash" in u:
        del u["password_hash"]
    return {"success": True, "user": u}


class AdminResetPasswordRequest(BaseModel):
    email: str
    password: str


@app.post("/api/admin/app-users/reset-password")
async def admin_reset_app_user_password(body: AdminResetPasswordRequest, _admin: dict = Depends(require_admin)):
    email = (body.email or "").strip().lower()
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    import bcrypt
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        permissions_db.set_app_user_password(email, pw_hash, must_change_password=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True}


class AdminDisableUserRequest(BaseModel):
    email: str
    disabled: bool = True


@app.post("/api/admin/app-users/disable")
async def admin_disable_app_user(body: AdminDisableUserRequest, _admin: dict = Depends(require_admin)):
    try:
        permissions_db.set_app_user_disabled(body.email, disabled=bool(body.disabled))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True}


# ======================= Admin: extensions & trunks =======================

_EXT_NAME_TRAIL_RE = re.compile(r"\s*<[^<>]+>\s*$")
_EXT_HTML_TAG_RE = re.compile(r"<[^<>]+>")


def _strip_ext_display_name(raw: str) -> str:
    """Extract the human label out of a MikoPBX select entry.

    ``getForSelect`` packs employee names together with HTML icon tags and a
    trailing ``<extension>`` marker, e.g.
    ``"<i class=\"user outline icon\"></i> User Name <201>"``. We remove all
    inline HTML, then drop the trailing ``<…>`` extension marker so the UI is
    left with just ``User Name``.
    """
    text = _EXT_HTML_TAG_RE.sub("", raw or "").strip()
    # Drop localisation-style prefix "Employee: User Name" -> "User Name"
    if ":" in text:
        head, _, tail = text.partition(":")
        if tail.strip() and head.strip():
            text = tail.strip()
    return _EXT_NAME_TRAIL_RE.sub("", text).strip()


@app.get("/api/extensions")
async def list_extensions(_admin: dict = Depends(require_admin)):
    """List internal extensions from MikoPBX, categorised by type.

    When REST is enabled we use ``extensions:getForSelect`` which returns a
    ``type`` (``USER``/``CONFERENCE``/``QUEUE``/``IVR_MENU``/
    ``DIALPLAN_APPLICATION``/``SYSTEM``) and a pre-formatted display name for
    each item. Mobile entries look the same as USER items but carry a mobile
    icon class — we surface that via an ``is_mobile`` flag so the admin UI can
    keep them out of the "PBX Users" editor while still showing them in the
    System tab.

    The response keeps the legacy ``{extension, name}`` pair untouched so old
    clients (and the legacy SQLite branch) keep working.
    """
    if _rest_enabled():
        try:
            items = await miko_rest.list_extensions_for_select(type_="SIP")
        except MikoRestError as exc:
            raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc

        extensions: list[dict] = []
        for i in items:
            ext = str(i.get("value") or "").strip()
            if not ext:
                continue
            raw_name = str(i.get("name") or "")
            display = _strip_ext_display_name(raw_name) or ext
            ext_type = (i.get("type") or "").strip().upper() or "SIP"
            # Mobile variants share the USER type with desk phones but are
            # rendered with a dedicated "mobile" icon class. MikoPBX is
            # authoritative here — never guess from extension length (real
            # internal users can legitimately be 8+ digits, e.g. 12345678).
            is_mobile = "mobile icon" in raw_name.lower()
            extensions.append({
                "extension": ext,
                "name": display,
                "type": ext_type,
                "type_localized": (i.get("typeLocalized") or "").strip(),
                "is_mobile": is_mobile,
            })
        extensions.sort(key=lambda x: (len(x["extension"]), x["extension"]))
        return {"extensions": extensions}

    config_db = cfg["config_db_path"]
    if not Path(config_db).exists():
        raise HTTPException(500, f"MikoPBX config database not found: {config_db}")

    conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT extension, description FROM m_Sip "
        "WHERE type='peer' AND disabled='0' "
        "ORDER BY CAST(extension AS INTEGER)"
    ).fetchall()
    conn.close()

    # Legacy SQLite path has no type info. Treat every SIP peer as a USER;
    # flag clearly phone-shaped numbers (>= 10 digits) as mobile so the split
    # still works when REST is off. Internal SIP user IDs in the 4–8 digit
    # range stay on the Users tab.
    extensions = []
    for r in rows:
        ext = r["extension"]
        is_mobile = ext.isdigit() and len(ext) >= 10
        extensions.append({
            "extension": ext,
            "name": r["description"] or ext,
            "type": "USER",
            "type_localized": "",
            "is_mobile": is_mobile,
        })
    return {"extensions": extensions}


def _miko_sip_trunks(config_db: str) -> list[dict]:
    """Active SIP trunks (type friend) from MikoPBX m_Sip."""
    conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, uniqid, description, fromuser, host, username, disablefromuser "
        "FROM m_Sip WHERE type='friend' AND disabled='0' "
        "ORDER BY COALESCE(description, ''), uniqid"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _rest_sip_trunks_full() -> list[dict]:
    """Compose the same dict shape as ``_miko_sip_trunks`` but from REST.

    N+1 calls because ``fromuser`` / ``disablefromuser`` only live on the full
    Provider payload — still fast for the typical 1-20 trunk case and keeps
    the UI response format identical between SQLite and REST deployments.
    """
    providers = await miko_rest.list_sip_providers()
    out: list[dict] = []
    for p in providers:
        if p.get("disabled"):
            continue
        pid = (p.get("id") or "").strip()
        if not pid:
            continue
        full = await miko_rest.get_sip_provider(pid) or {}
        out.append({
            "id": pid,
            "uniqid": pid,
            "description": full.get("description") or p.get("description") or pid,
            "fromuser": (full.get("fromuser") or "").strip(),
            "host": full.get("host") or p.get("host") or "",
            "username": full.get("username") or p.get("username") or "",
            "disablefromuser": "1" if full.get("disablefromuser") else "0",
        })
    out.sort(key=lambda r: (r.get("description") or "", r.get("uniqid") or ""))
    return out


@app.get("/api/trunk-callerids")
async def list_trunk_callerids(_admin: dict = Depends(require_admin)):
    """CallerID numbers: MikoPBX fromuser on trunks + manually added per trunk (proxy DB)."""
    callerids: list[dict] = []
    if _rest_enabled():
        try:
            trunks = await _rest_sip_trunks_full()
        except MikoRestError as exc:
            raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc
        seen_numbers: set[str] = set()
        for t in trunks:
            num = (t.get("fromuser") or "").strip()
            if num and num not in seen_numbers:
                seen_numbers.add(num)
                callerids.append({
                    "number": num,
                    "description": t.get("description") or num,
                    "source": "miko",
                })
    else:
        config_db = cfg["config_db_path"]
        if not Path(config_db).exists():
            raise HTTPException(500, f"MikoPBX config database not found: {config_db}")

        conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT fromuser, description FROM m_Sip "
            "WHERE type='friend' AND disabled='0' "
            "AND fromuser IS NOT NULL AND TRIM(fromuser) != '' "
            "ORDER BY fromuser"
        ).fetchall()
        conn.close()
        callerids = [
            {"number": r["fromuser"], "description": r["description"] or r["fromuser"], "source": "miko"}
            for r in rows
        ]

    seen = {c["number"] for c in callerids}
    for num in permissions_db.get_all_manual_trunk_callerid_numbers():
        if num not in seen:
            seen.add(num)
            callerids.append({"number": num, "description": num, "source": "manual"})
    callerids.sort(key=lambda x: x["number"])
    return {"callerids": callerids}


@app.get("/api/admin/trunks")
async def admin_list_trunks(_admin: dict = Depends(require_admin)):
    """SIP trunks from MikoPBX for the Trunks admin page."""
    if _rest_enabled():
        try:
            return {"trunks": await _rest_sip_trunks_full()}
        except MikoRestError as exc:
            raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc

    config_db = cfg["config_db_path"]
    if not Path(config_db).exists():
        raise HTTPException(500, f"MikoPBX config database not found: {config_db}")
    return {"trunks": _miko_sip_trunks(config_db)}


# ----- Extra REST-only admin endpoints (no SQLite equivalent) -----

@app.get("/api/admin/provider-statuses")
async def admin_provider_statuses(_admin: dict = Depends(require_admin)):
    """Live registration status for SIP providers (requires REST API).

    Surfaces the same data the MikoPBX web UI shows in Trunks → Status, so the
    admin panel can warn when a provider is offline (the exact symptom we hit
    when the disabled trunk swallowed every outbound call).
    """
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        return {"statuses": await miko_rest.get_sip_provider_statuses()}
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc


@app.get("/api/admin/active-calls")
async def admin_active_calls(_admin: dict = Depends(require_admin)):
    """Currently active calls on the PBX (requires REST API)."""
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        return {"calls": await miko_rest.get_active_calls()}
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc


@app.get("/api/admin/active-channels")
async def admin_active_channels(_admin: dict = Depends(require_admin)):
    """Low-level active Asterisk channels (codec, RTP, bridge partner).

    Complements ``/api/admin/active-calls`` which is the high-level call view;
    this one exposes per-channel codec and RTP metrics that let the admin
    spot packet loss / jitter in live calls.
    """
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        return {"channels": await miko_rest.get_active_channels()}
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc


def _classify_peer_status(state: str | None) -> str:
    """Map varied MikoPBX peer-state strings to a compact traffic-light class.

    Different MikoPBX builds emit slightly different words here (``OK`` vs
    ``Reachable``, ``LAGGED`` vs ``Lagged``, etc.), so we normalise up front
    and keep the frontend dumb: one of ``up``/``lag``/``down``/``off``/``unknown``.
    """
    raw = (state or "").strip().lower()
    if not raw:
        return "unknown"
    if raw in ("off", "disabled"):
        return "off"
    if "lag" in raw:
        return "lag"
    if raw in ("ok", "reachable", "registered", "available"):
        return "up"
    if raw in ("unregistered", "unreachable", "rejected", "failed", "error", "unavailable"):
        return "down"
    if raw == "unknown":
        return "unknown"
    # Fallback: treat known-safe words as UP, otherwise unknown.
    return "unknown"


@app.get("/api/admin/sip-peers")
async def admin_sip_peers(_admin: dict = Depends(require_admin)):
    """Live registration status for every SIP peer (users + trunks).

    This is the single source of truth for the admin-panel health widget:
    it cross-references MikoPBX's per-peer status with the known trunk list
    so the UI can label each row as "Trunk: Provider Name" or "Ext 201".
    """
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        peers = await miko_rest.get_sip_peers_statuses()
        # Trunk registration lives on /sip-providers:getStatuses (state=registered),
        # not /sip:getStatuses (OPTIONS reachability for endpoints). Merging both
        # avoids UNKNOWN on IP-auth trunks such as Telnyx.
        provider_status_by_id: dict[str, dict] = {}
        try:
            for row in await miko_rest.get_sip_provider_statuses():
                pid = str(row.get("id") or row.get("uniqid") or "").strip()
                if pid:
                    provider_status_by_id[pid] = row
        except MikoRestError:
            provider_status_by_id = {}
        # Best-effort trunk join so the widget can show friendly labels next
        # to trunk IDs (they're stored as numeric uniqids in pjsip).
        trunks_by_id: dict[str, dict] = {}
        try:
            for t in await miko_rest.list_sip_providers():
                if t.get("disabled"):
                    continue
                uid = str(t.get("id") or t.get("uniqid") or "").strip()
                if uid:
                    trunks_by_id[uid] = t
        except Exception:
            trunks_by_id = {}
    except MikoRestError as exc:
        # Log so we can tell whether MikoPBX renamed/removed the endpoint on a
        # given build — otherwise the admin widget just silently hides itself.
        print(
            f"[sip-peers] MikoRestError status={exc.status} message={exc.message} "
            f"payload={str(exc.payload)[:200]}",
            flush=True,
        )
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc

    def _peer_state(row: dict) -> str:
        return str(
            row.get("state")
            or row.get("status")
            or row.get("reg_state")
            or row.get("Status")
            or ""
        ).strip()

    enriched: list[dict] = []
    seen_ids: set[str] = set()
    for row in peers:
        pid = str(row.get("id") or row.get("peer") or row.get("name") or "").strip()
        if not pid:
            continue
        trunk = trunks_by_id.get(pid)
        provider = provider_status_by_id.get(pid)
        kind = "trunk" if (trunk or provider) else "user"
        if kind == "trunk" and provider:
            state = _peer_state(provider) or _peer_state(row)
            src = provider
        else:
            state = _peer_state(row)
            src = row
        label = ""
        if trunk:
            label = str(trunk.get("description") or trunk.get("host") or pid)
        elif provider:
            label = str(provider.get("description") or provider.get("host") or pid)
        enriched.append({
            "id": pid,
            "kind": kind,
            "label": label,
            "state": state,
            "state_class": _classify_peer_status(state),
            # Pass-through raw fields that may be useful in the UI.
            "ip": src.get("ip") or src.get("host") or "",
            "time_response": src.get("time_response") or src.get("rtt") or "",
        })
        seen_ids.add(pid)

    for pid, provider in provider_status_by_id.items():
        if pid in seen_ids:
            continue
        trunk = trunks_by_id.get(pid)
        if not trunk and not provider.get("description"):
            continue
        state = _peer_state(provider)
        enriched.append({
            "id": pid,
            "kind": "trunk",
            "label": str(
                (trunk or {}).get("description")
                or provider.get("description")
                or provider.get("host")
                or pid
            ),
            "state": state,
            "state_class": _classify_peer_status(state),
            "ip": provider.get("ip") or provider.get("host") or "",
            "time_response": provider.get("time_response") or provider.get("rtt") or "",
        })
    return {"peers": enriched}


@app.post("/api/admin/sip/force-status-check")
async def admin_force_sip_status_check(_admin: dict = Depends(require_admin)):
    """Ask MikoPBX to immediately re-probe all SIP peers (OPTIONS)."""
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        ok = await miko_rest.force_sip_status_check()
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc
    return {"success": bool(ok)}


# ======================= SIP auth failure warning =======================

@app.get("/api/sip-auth-failures")
async def get_sip_auth_failures(
    ext: str | None = Query(None, description="Extension to inspect. Non-admins can only see their own."),
    user: dict = Depends(require_jwt),
):
    """Return recent SIP auth-failure stats for an extension.

    The softphone polls this endpoint before/after registration so we can warn
    the user ("your credentials are being tried elsewhere" / "wrong password")
    *before* MikoPBX's Fail2Ban bans the source IP. Regular users may only
    inspect their own extension; admins may pass any ``ext``.
    """
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")

    is_admin = (user.get("role") == "admin")
    caller_ext = _extension_from_token(user)
    target_ext = (ext or "").strip()
    if not target_ext:
        target_ext = caller_ext
    if not target_ext:
        raise HTTPException(400, "Extension is required")
    if not is_admin and target_ext != caller_ext:
        raise HTTPException(403, "You may only query auth failures for your own extension")

    try:
        stats = await miko_rest.get_sip_auth_failure_stats()
    except MikoRestError as exc:
        raise HTTPException(502, f"MikoPBX REST error: {exc.message}") from exc

    # ``None`` means the MikoPBX build doesn't expose this endpoint at all.
    # Return 501 so the softphone treats it as "feature unavailable" and
    # silently stops polling — without spamming the log with 502s.
    if stats is None:
        raise HTTPException(501, "MikoPBX REST build does not expose SIP auth-failure stats.")

    # MikoPBX's response shape varies: sometimes a flat list of failures,
    # sometimes a dict keyed by username, sometimes aggregated counters.
    failures_for_ext: list[dict] = []
    total_failures = 0
    candidate_lists: list[list] = []
    if isinstance(stats, dict):
        for key in ("failures", "stats", "items", "data"):
            value = stats.get(key)
            if isinstance(value, list):
                candidate_lists.append(value)
            elif isinstance(value, dict):
                # Dict keyed by username/IP.
                per_user = value.get(target_ext)
                if isinstance(per_user, list):
                    candidate_lists.append(per_user)
                elif isinstance(per_user, dict):
                    candidate_lists.append([per_user])

    for lst in candidate_lists:
        for item in lst:
            if not isinstance(item, dict):
                continue
            total_failures += 1
            user_field = str(
                item.get("username")
                or item.get("user")
                or item.get("peer")
                or item.get("name")
                or ""
            ).strip()
            if user_field == target_ext:
                failures_for_ext.append(item)

    return {
        "extension": target_ext,
        "failures_for_extension": failures_for_ext,
        "failure_count": len(failures_for_ext),
        "total_failures": total_failures,
        # Raw payload is handy for admins investigating, but regular users
        # probably shouldn't see other people's failed attempts — only return
        # it to admins.
        "raw": stats if is_admin else None,
    }


# ======================= WPF auto-provisioning =======================

class ProvisionGenerateRequest(BaseModel):
    extension: str
    ttl_minutes: int | None = None


@app.post("/api/admin/provision/generate")
async def admin_generate_provision_token(
    body: ProvisionGenerateRequest,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Create a one-time token the admin hands to an end user.

    The admin panel converts the response into a ``callspire://`` URL so the
    desktop app can self-configure without the user ever copy-pasting a
    password. The token is opaque, single-use and expires after ``ttl_minutes``
    (default 30m, max 24h).
    """
    extension = (body.extension or "").strip()
    if not extension:
        raise HTTPException(400, "extension is required")
    try:
        token = permissions_db.create_provision_token(
            extension,
            ttl_minutes=body.ttl_minutes or 30,
            created_by=str(admin.get("sub") or admin.get("username") or "admin"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The callspire:// URL the admin will send to the user. We build it from
    # the incoming request so reverse-proxied deployments "just work".
    return {
        "token_id": token["token_id"],
        "extension": token["extension"],
        "expires_at": token["expires_at"],
        "ttl_minutes": token["ttl_minutes"],
        # Authoritative redeem base for callspire:// links (honours public_url + X-Forwarded-Prefix).
        "proxy_url": _external_base_url(request),
    }


@app.get("/api/provision/redeem")
async def redeem_provision_token(token: str = Query(..., description="Opaque token_id from callspire:// URL")):
    """Exchange a provisioning token for SIP credentials, single-use.

    No JWT required — the token itself is the authenticator (256 bits of
    entropy, one-shot). We consume the token atomically *before* fetching the
    secret so a replay attack can't double-spend even if MikoPBX is slow.
    """
    row = permissions_db.consume_provision_token(token)
    if row is None:
        # Deliberately generic to avoid leaking which of "expired/used/unknown" applies.
        raise HTTPException(404, "Invalid or expired provisioning token")

    extension = row["extension"]

    # SIP secret: prefer MikoPBX REST (handles non-pjsip backends too),
    # fall back to reading the secret straight from pjsip.conf on the host.
    sip_password = ""
    if _rest_enabled():
        try:
            sip_password = await miko_rest.get_sip_secret(extension)
        except MikoRestError as exc:
            print(f"[provision] miko_rest.get_sip_secret failed for {extension}: {exc}", flush=True)
    if not sip_password:
        sip_password = get_pjsip_peer_secret(
            extension,
            path=cfg.get("mikopbx_pjsip_conf_path") or "",
            container=cfg.get("mikopbx_docker_container") or "",
            cache_ttl_seconds=int(cfg.get("mikopbx_pjsip_cache_seconds") or 60),
        )
    if not sip_password:
        raise HTTPException(502, "Unable to fetch SIP secret from MikoPBX")

    # Derive a reasonable SIP server hostname for the softphone to use.
    # Order matters: the REST URL is almost always a loopback address (the
    # proxy typically runs on the MikoPBX host itself), so we can't hand it
    # to a remote softphone. Prefer the operator-configured public ``sip_host``
    # (same value the WebRTC admin tab uses — guaranteed externally routable),
    # fall back to ``public_url``'s hostname, and only then to the REST URL's
    # hostname if it isn't a loopback/link-local address.
    wrtc = permissions_db.get_webrtc_public_config()
    ws_url = (wrtc.get("ws_url") or "").strip()
    pbx_host = (wrtc.get("sip_host") or "").strip()

    def _hostname_of(url: str) -> str:
        try:
            return urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return ""

    def _is_loopback_host(host: str) -> bool:
        h = (host or "").strip().lower()
        if not h:
            return True
        if h in ("localhost", "ip6-localhost", "ip6-loopback"):
            return True
        return h.startswith("127.") or h == "::1"

    if not pbx_host:
        public_host = _hostname_of((cfg.get("public_url") or "").strip())
        if public_host and not _is_loopback_host(public_host):
            pbx_host = public_host

    if not pbx_host:
        rest_host = _hostname_of((cfg.get("mikopbx_rest_url") or "").strip())
        if rest_host and not _is_loopback_host(rest_host):
            pbx_host = rest_host

    # Last-resort: honour the REST URL even if it's loopback — better than
    # leaving the client with an empty sip_server. Admin will see this in the
    # "setup complete" dialog and can fix it in Settings.
    if not pbx_host:
        pbx_host = _hostname_of((cfg.get("mikopbx_rest_url") or "").strip())

    kommo_hints: dict = {
        "configure_gateway": False,
        "offer_gateway": False,
        "upload_enabled": False,
        "excluded": False,
        "authorized": False,
        "kommo_user_name": "",
        "subdomain": "",
    }
    try:
        kommo_hints = await kommo_service.kommo_provision_hints_for_extension(
            jwt_secret=cfg["jwt_secret"],
            extension=extension,
        )
    except Exception as exc:
        print(f"[provision] kommo hints skipped for ext={extension}: {exc}", flush=True)

    return {
        "extension": extension,
        "sip_username": extension,
        "sip_password": sip_password,
        "sip_server": pbx_host,
        "sip_port": 5060,
        "ws_url": ws_url,
        # Tell the client which proxy issued this token so Settings can point
        # at the right host for CDR/permissions.
        "proxy_url": (cfg.get("public_url") or "").strip(),
        # When admin mapped a Kommo user and gateway OAuth is done, desktop can
        # auto-enable shared Kommo (no per-user OAuth on the softphone).
        "kommo": kommo_hints,
    }


@app.get("/api/admin/trunk-manual-numbers")
async def admin_get_trunk_manual_numbers(_admin: dict = Depends(require_admin)):
    return {"by_trunk": permissions_db.get_manual_callerids_by_trunk()}


class TrunkManualNumberRequest(BaseModel):
    trunk_uniqid: str
    callerid: str


@app.post("/api/admin/trunk-manual-numbers")
async def admin_add_trunk_manual_number(body: TrunkManualNumberRequest, _admin: dict = Depends(require_admin)):
    try:
        permissions_db.add_trunk_manual_callerid(body.trunk_uniqid, body.callerid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True}


@app.post("/api/admin/trunk-manual-numbers/remove")
async def admin_remove_trunk_manual_number(body: TrunkManualNumberRequest, _admin: dict = Depends(require_admin)):
    permissions_db.remove_trunk_manual_callerid(body.trunk_uniqid.strip(), body.callerid.strip())
    return {"success": True}


# ======================= Admin: permissions =======================

@app.get("/api/admin/permissions")
async def admin_get_permissions(_admin: dict = Depends(require_admin)):
    return {"permissions": permissions_db.get_all_permissions()}


class PermissionRequest(BaseModel):
    extension: str
    callerids: list[str]


@app.post("/api/admin/permissions")
async def admin_set_permissions(body: PermissionRequest, _admin: dict = Depends(require_admin)):
    permissions_db.set_allowed_callerids(body.extension, body.callerids)
    return {"success": True, "extension": body.extension, "callerids": body.callerids}


# ======================= Admin: CallerID names =======================

@app.get("/api/admin/callerid-names")
async def admin_get_callerid_names(_admin: dict = Depends(require_admin)):
    return {"names": permissions_db.get_all_callerid_names()}


class CallerIdNameRequest(BaseModel):
    number: str
    name: str = ""
    note: str = ""


@app.post("/api/admin/callerid-names")
async def admin_set_callerid_name(body: CallerIdNameRequest, _admin: dict = Depends(require_admin)):
    permissions_db.set_callerid_name(body.number, body.name, body.note)
    return {"success": True}


# ======================= Admin: AMI config =======================

@app.get("/api/admin/ami-config")
async def admin_get_ami_config(_admin: dict = Depends(require_admin)):
    c = permissions_db.get_ami_config()
    c["secret"] = "***" if c.get("secret") else ""
    return c


class AmiConfigRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5038
    username: str = ""
    secret: str | None = None


@app.post("/api/admin/ami-config")
async def admin_set_ami_config(body: AmiConfigRequest, _admin: dict = Depends(require_admin)):
    secret = body.secret
    if not secret:
        current = permissions_db.get_ami_config()
        secret = current.get("secret", "")
    permissions_db.set_ami_config(body.host, body.port, body.username, secret)
    return {"success": True}


# ======================= Admin: Web softphone WebRTC (WSS / SIP host) =======================

@app.get("/api/admin/webrtc-config")
async def admin_get_webrtc_config(_admin: dict = Depends(require_admin)):
    c = permissions_db.get_webrtc_public_config()
    return {"ws_url": c.get("ws_url") or "", "sip_host": c.get("sip_host") or ""}


class WebrtcPublicConfigRequest(BaseModel):
    ws_url: str = ""
    sip_host: str = ""


@app.post("/api/admin/webrtc-config")
async def admin_set_webrtc_config(body: WebrtcPublicConfigRequest, _admin: dict = Depends(require_admin)):
    permissions_db.set_webrtc_public_config(body.ws_url.strip(), body.sip_host.strip())
    return {"success": True}


# ======================= Admin: Softphone app settings (desktop TURN) =======================

@app.get("/api/admin/softphone-app-settings")
async def admin_get_softphone_app_settings(_admin: dict = Depends(require_admin)):
    return permissions_db.get_softphone_app_settings()


class SoftphoneAppSettingsRequest(BaseModel):
    turn_uri: str = ""
    turn_username: str = ""
    turn_password: str | None = None


@app.post("/api/admin/softphone-app-settings")
async def admin_set_softphone_app_settings(
    body: SoftphoneAppSettingsRequest, _admin: dict = Depends(require_admin)
):
    result = permissions_db.set_softphone_app_settings(
        turn_uri=body.turn_uri.strip(),
        turn_username=body.turn_username.strip(),
        turn_password=body.turn_password,
    )
    return {"success": True, **result}



# ======================= Admin: JWT / session lifetime =======================

@app.get("/api/admin/jwt-config")
async def admin_get_jwt_config(_admin: dict = Depends(require_admin)):
    days = _jwt_expire_days()
    return {
        "expire_days": days,
        "never_expire": days == 0,
    }


class JwtConfigRequest(BaseModel):
    expire_days: int = 30
    never_expire: bool = False


@app.post("/api/admin/jwt-config")
async def admin_set_jwt_config(body: JwtConfigRequest, _admin: dict = Depends(require_admin)):
    from config import set_jwt_expire_days

    days = 0 if body.never_expire else max(1, min(int(body.expire_days), 3650))
    cfg["jwt_expire_days"] = set_jwt_expire_days(days)
    return {
        "success": True,
        "expire_days": cfg["jwt_expire_days"],
        "never_expire": cfg["jwt_expire_days"] == 0,
    }
@app.get("/api/admin/connection-status")
async def admin_connection_status(_admin: dict = Depends(require_admin)):
    """TCP/TLS reachability from this server (not a full SIP register or WebSocket handshake)."""
    ami = permissions_db.get_ami_config()
    ami_ok, ami_detail = check_ami_tcp(ami.get("host") or "127.0.0.1", ami.get("port") or 5038)

    wrtc = permissions_db.get_webrtc_public_config()
    ws_url = (wrtc.get("ws_url") or "").strip()
    if not ws_url:
        webrtc_ok, webrtc_detail = False, "WSS URL is not set."
    else:
        webrtc_ok, webrtc_detail = check_wss_tls(ws_url)

    return {
        "ami": {"ok": ami_ok, "detail": ami_detail},
        "webrtc": {"ok": webrtc_ok, "detail": webrtc_detail},
    }


# ======================= Admin: Kommo CRM =======================

class KommoConfigRequest(BaseModel):
    enabled: bool = False
    client_id: str = ""
    client_secret: str | None = None
    redirect_uri: str = ""
    domain: str = ""


class KommoExtensionUserRequest(BaseModel):
    extension: str
    kommo_user_id: int | None = None
    kommo_user_name: str = ""


class KommoExcludedRequest(BaseModel):
    excluded_extensions: list[str] = []


class KommoEntityRuleRequest(BaseModel):
    id: int | None = None
    did: str = ""
    call_type: str
    entity_action: str = "none"
    pipeline_id: int | None = None
    status_id: int | None = None
    default_responsible_user_id: int | None = None
    contact_name_template: str = ""
    lead_name_template: str = ""
    lead_tag_id: int | None = None
    lead_tag_name: str = ""
    create_task: bool = False
    task_name_template: str = ""
    task_responsible: str = "client_owner"
    task_responsible_user_id: int | None = None
    task_deadline_hours: int = 1
    enabled: bool = True
    sort_order: int = 0


@app.get("/api/admin/kommo-config")
async def admin_get_kommo_config(request: Request, _admin: dict = Depends(require_admin)):
    return kommo_service.get_admin_config(
        jwt_secret=cfg["jwt_secret"],
        default_redirect_uri=_kommo_default_redirect_uri(request),
    )


@app.post("/api/admin/kommo-config")
async def admin_save_kommo_config(body: KommoConfigRequest, request: Request, _admin: dict = Depends(require_admin)):
    kommo_service.save_admin_config(
        jwt_secret=cfg["jwt_secret"],
        enabled=body.enabled,
        client_id=body.client_id,
        client_secret=body.client_secret,
        redirect_uri=body.redirect_uri,
        subdomain=body.domain,
    )
    return {"success": True}


@app.post("/api/admin/kommo/oauth/start")
async def admin_kommo_oauth_start(request: Request, _admin: dict = Depends(require_admin)):
    try:
        return kommo_service.start_oauth(
            jwt_secret=cfg["jwt_secret"],
            default_redirect_uri=_kommo_default_redirect_uri(request),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/kommo/test")
async def admin_kommo_test(_admin: dict = Depends(require_admin)):
    return await kommo_service.admin_test_kommo(jwt_secret=cfg["jwt_secret"])


@app.post("/api/admin/kommo/disconnect")
async def admin_kommo_disconnect(_admin: dict = Depends(require_admin)):
    import permissions_db as _pdb
    _pdb.set_kommo_oauth_tokens(
        access_token="",
        refresh_token="",
        expires_at="",
        referer="",
        account_base_url="",
    )
    return {"success": True}


@app.get("/api/admin/kommo-users")
async def admin_kommo_users(_admin: dict = Depends(require_admin)):
    try:
        return await kommo_service.list_admin_kommo_users(jwt_secret=cfg["jwt_secret"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/kommo-users/sync")
async def admin_kommo_users_sync(_admin: dict = Depends(require_admin)):
    """Fetch current Kommo users from AmoCRM; refresh stored names; keep extension mappings."""
    try:
        return await kommo_service.sync_admin_kommo_users(jwt_secret=cfg["jwt_secret"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/kommo-extension-user")
async def admin_kommo_extension_user(body: KommoExtensionUserRequest, _admin: dict = Depends(require_admin)):
    kommo_service.save_kommo_extension_user(
        extension=body.extension,
        kommo_user_id=body.kommo_user_id,
        kommo_user_name=body.kommo_user_name,
    )
    return {"success": True}


@app.post("/api/admin/kommo-excluded-extensions")
async def admin_kommo_excluded(body: KommoExcludedRequest, _admin: dict = Depends(require_admin)):
    kommo_service.save_kommo_excluded_extensions(body.excluded_extensions)
    return {"success": True}


@app.get("/api/admin/kommo/entity-rules/meta")
async def admin_kommo_entity_rules_meta(_admin: dict = Depends(require_admin)):
    from kommo_entity_rules import CALL_TYPES, ENTITY_ACTIONS, TASK_RESPONSIBLE_MODES

    return {
        "call_types": CALL_TYPES,
        "entity_actions": ENTITY_ACTIONS,
        "task_responsible_modes": TASK_RESPONSIBLE_MODES,
    }


@app.get("/api/admin/kommo/entity-rules")
async def admin_list_kommo_entity_rules(_admin: dict = Depends(require_admin)):
    return {"rules": permissions_db.list_kommo_entity_rules()}


@app.post("/api/admin/kommo/entity-rules")
async def admin_save_kommo_entity_rule(
    body: KommoEntityRuleRequest, _admin: dict = Depends(require_admin)
):
    try:
        saved = permissions_db.upsert_kommo_entity_rule(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True, "rule": saved}


@app.delete("/api/admin/kommo/entity-rules/{rule_id}")
async def admin_delete_kommo_entity_rule(rule_id: int, _admin: dict = Depends(require_admin)):
    if not permissions_db.delete_kommo_entity_rule(rule_id):
        raise HTTPException(404, "Rule not found")
    return {"success": True}


@app.get("/api/admin/kommo/pipelines")
async def admin_kommo_pipelines(_admin: dict = Depends(require_admin)):
    tokens = permissions_db.get_kommo_oauth_tokens()
    if not tokens.get("access_token"):
        raise HTTPException(400, "Kommo is not authorized on gateway")
    cfg_row = permissions_db.get_kommo_integration()
    from kommo_crm import KommoCrmClient
    from secret_vault import decrypt_secret

    access = decrypt_secret(tokens.get("access_token") or "", cfg["jwt_secret"])
    if not access:
        raise HTTPException(400, "Kommo access token is missing")
    account_base = (tokens.get("account_base_url") or "").strip().rstrip("/")
    subdomain = (cfg_row.get("subdomain") or "").strip()
    client = KommoCrmClient(
        subdomain,
        access,
        account_base_url=account_base or None,
    )
    try:
        pipelines = await client.list_pipelines()
    finally:
        await client.close()
    return {"pipelines": pipelines}


@app.get("/api/admin/kommo/tags")
async def admin_kommo_lead_tags(
    query: str = "",
    _admin: dict = Depends(require_admin),
):
    tokens = permissions_db.get_kommo_oauth_tokens()
    if not tokens.get("access_token"):
        raise HTTPException(400, "Kommo is not authorized on gateway")
    cfg_row = permissions_db.get_kommo_integration()
    from kommo_crm import KommoCrmClient
    from secret_vault import decrypt_secret

    access = decrypt_secret(tokens.get("access_token") or "", cfg["jwt_secret"])
    if not access:
        raise HTTPException(400, "Kommo access token is missing")
    account_base = (tokens.get("account_base_url") or "").strip().rstrip("/")
    subdomain = (cfg_row.get("subdomain") or "").strip()
    client = KommoCrmClient(
        subdomain,
        access,
        account_base_url=account_base or None,
    )
    try:
        tags = await client.list_lead_tags(query=query)
    finally:
        await client.close()
    return {"tags": tags, "count": len(tags)}


register_kommo_routes(
    app,
    cfg=cfg,
    require_admin=require_admin,
    require_jwt=require_jwt,
    templates=templates,
    html_context=_html_context,
    kommo_default_redirect_uri=_kommo_default_redirect_uri,
    query_cdr=_kommo_internal_query_cdr,
    download_recording=_kommo_internal_download_recording,
    verify_linkedid=_kommo_verify_linkedid,
    extension_from_token=_extension_from_token,
)
# ======================= Admin panel (HTML) =======================

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", context=_html_context(request))



# ======================= Browser softphone (SPA + /api session) =======================
# After all API routes. Optional: WEB_SOFTPHONE_ENABLED=0, WEB_SOFTPHONE_SKIP_ROOT_REDIRECT=1
if _install_web_softphone is not None:
    _install_web_softphone(app)
else:
    print("[pbx-gateway] Web softphone: disabled (gateway_web_softphone package not installed)", flush=True)
# ======================= Run =======================

def main():
    try:
        uvicorn.run(
        "app:app",
        host=cfg["host"],
        port=cfg["port"],
        ssl_certfile=cfg.get("ssl_certfile"),
        ssl_keyfile=cfg.get("ssl_keyfile"),
        log_level="info",
        )
    except Exception as exc:
        import traceback
        print(f"[pbx-gateway] FATAL startup error: {exc}", flush=True)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1)
