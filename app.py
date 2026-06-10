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
import sqlite3
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import hmac

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
)
from config import load_config
import permissions_db
from pjsip_secrets import get_peer_secret as get_pjsip_peer_secret
from ami_client import ami_originate
from connection_check import check_ami_tcp, check_wss_tls
from miko_rest_client import MikoRestClient, MikoRestConfig, MikoRestError
from app_kommo import register_kommo_routes

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

    if _rest_enabled():
        try:
            trunk_cids = await load_trunk_callerids_rest(miko_rest)
            records = await query_cdr_rest(
                miko_rest,
                trunk_cids,
                ext=ext_filter,
                dst=dst,
                start_from=start_from,
                start_to=start_to,
                limit=limit,
                offset=offset,
            )
        except MikoRestError as exc:
            raise HTTPException(502, f"MikoPBX REST CDR error: {exc.message}") from exc
        except Exception as exc:
            raise HTTPException(502, f"CDR query error: {exc}") from exc
        return {"result": True, "data": records}

    try:
        records = query_cdr(
            cdr_db_path=cfg["cdr_db_path"],
            config_db_path=cfg["config_db_path"],
            ext=ext_filter,
            dst=dst,
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=offset,
            **_cdr_docker_kwargs(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"CDR query error: {exc}")

    return {"result": True, "data": records}


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


# ======================= CallerID / Originate =======================


def _normalize_originate_number(raw: str) -> str:
    """Strip spaces/dashes; E.164 with + when user typed + or length >= 11 digits (short extensions unchanged)."""
    t = (raw or "").strip()
    if not t:
        return ""
    digits = "".join(c for c in t if c.isdigit())
    if not digits:
        return t
    if t.startswith("+") or len(digits) >= 11:
        return f"+{digits}"
    return digits


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

    ring_raw = (body.ring_extension or "").strip()
    if ring_raw.upper().endswith("-WS"):
        ring_raw = ring_raw[:-3]
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
    return {"success": True}


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
    }
    if sip_password:
        out["sipAuthUser"] = sip_auth_user
    return out


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
    ``"<i class=\"user outline icon\"></i> Smith James <201>"``. We remove all
    inline HTML, then drop the trailing ``<…>`` extension marker so the UI is
    left with just ``Smith James``.
    """
    text = _EXT_HTML_TAG_RE.sub("", raw or "").strip()
    # Drop localisation-style prefix "Сотрудник: Smith James" -> "Smith James"
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
            # internal users can legitimately be 8+ digits, e.g. 52678419).
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
    if raw in ("ok", "reachable", "registered"):
        return "up"
    if raw in ("unreachable", "unknown", "rejected", "failed", "error"):
        return "down"
    # Fallback: treat known-safe words as UP, otherwise unknown.
    return "unknown"


@app.get("/api/admin/sip-peers")
async def admin_sip_peers(_admin: dict = Depends(require_admin)):
    """Live registration status for every SIP peer (users + trunks).

    This is the single source of truth for the admin-panel health widget:
    it cross-references MikoPBX's per-peer status with the known trunk list
    so the UI can label each row as "Trunk: Twilio US" or "Ext 201".
    """
    if not _rest_enabled():
        raise HTTPException(501, "MikoPBX REST API is not enabled (set use_rest_api: true).")
    try:
        peers = await miko_rest.get_sip_peers_statuses()
        # Best-effort trunk join so the widget can show friendly labels next
        # to trunk IDs (they're stored as numeric uniqids in pjsip).
        trunks_by_id: dict[str, dict] = {}
        try:
            for t in await _rest_sip_trunks_full():
                uid = str(t.get("uniqid") or t.get("id") or "").strip()
                if uid:
                    trunks_by_id[uid] = t
        except Exception:
            # Non-fatal: the widget still works without labels.
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

    enriched: list[dict] = []
    for row in peers:
        pid = str(row.get("id") or row.get("peer") or row.get("name") or "").strip()
        state = (
            row.get("state")
            or row.get("status")
            or row.get("reg_state")
            or row.get("Status")
            or ""
        )
        trunk = trunks_by_id.get(pid)
        kind = "trunk" if trunk else "user"
        label = ""
        if trunk:
            label = str(trunk.get("description") or trunk.get("host") or pid)
        enriched.append({
            "id": pid,
            "kind": kind,
            "label": label,
            "state": state,
            "state_class": _classify_peer_status(state),
            # Pass-through raw fields that may be useful in the UI.
            "ip": row.get("ip") or row.get("host") or "",
            "time_response": row.get("time_response") or row.get("rtt") or "",
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
async def admin_generate_provision_token(body: ProvisionGenerateRequest, admin: dict = Depends(require_admin)):
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



register_kommo_routes(
    app,
    cfg=cfg,
    require_admin=require_admin,
    require_jwt=require_jwt,
    templates=templates,
    html_context=_html_context,
    kommo_default_redirect_uri=_kommo_default_redirect_uri,
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
