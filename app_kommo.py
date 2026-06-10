"""Kommo / AmoCRM HTTP routes for the PBX gateway.

Wire into ``app.py`` (after ``cfg``, ``require_admin``, ``require_jwt`` exist)::

    from app_kommo import register_kommo_routes
    register_kommo_routes(
        app,
        cfg=cfg,
        require_admin=require_admin,
        require_jwt=require_jwt,
        templates=templates,
        html_context=_html_context,
        kommo_default_redirect_uri=_kommo_default_redirect_uri,
    )
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import kommo_service


class KommoTestRequest(BaseModel):
    access_token: str | None = None


class KommoExcludedRequest(BaseModel):
    excluded_extensions: list[str] = Field(default_factory=list)


class KommoConfigRequest(BaseModel):
    enabled: bool = False
    client_id: str = ""
    client_secret: str | None = None
    redirect_uri: str = ""
    subdomain: str = ""
    domain: str = ""


class KommoExtensionUserRequest(BaseModel):
    extension: str = ""
    kommo_user_id: int | None = None
    kommo_user_name: str = ""


def _jwt_extension(user: dict) -> str:
    """Match ``app._extension_from_token`` — JWT may use ``ext`` or numeric ``sub``."""
    ext = str(user.get("extension") or user.get("ext") or "").strip()
    if ext:
        return ext
    sub = str(user.get("sub") or "").strip()
    if sub and "@" not in sub:
        return sub
    return ""


def register_kommo_routes(
    app,
    *,
    cfg: dict,
    require_admin: Callable,
    require_jwt: Callable,
    templates,
    html_context: Callable[..., dict],
    kommo_default_redirect_uri: Callable[[Request], str],
) -> None:
    jwt_secret = cfg["jwt_secret"]

    @app.get("/api/admin/kommo-config")
    async def admin_get_kommo_config(request: Request, _admin: dict = Depends(require_admin)):
        return kommo_service.get_admin_config(
            jwt_secret=jwt_secret,
            default_redirect_uri=kommo_default_redirect_uri(request),
        )

    @app.post("/api/admin/kommo-config")
    async def admin_set_kommo_config(
        body: KommoConfigRequest,
        request: Request,
        _admin: dict = Depends(require_admin),
    ):
        try:
            redirect = body.redirect_uri.strip() or kommo_default_redirect_uri(request)
            domain_value = (body.domain or body.subdomain or "").strip()
            kommo_service.save_admin_config(
                jwt_secret=jwt_secret,
                enabled=body.enabled,
                client_id=body.client_id,
                client_secret=body.client_secret,
                redirect_uri=redirect,
                subdomain=domain_value or None,
            )
            return {"ok": True}
        except Exception as exc:
            print(f"[kommo] save config failed: {exc}", flush=True)
            raise HTTPException(500, f"Failed to save Kommo settings: {exc}") from exc

    @app.post("/api/admin/kommo-excluded-extensions")
    async def admin_set_kommo_excluded_extensions(
        body: KommoExcludedRequest,
        _admin: dict = Depends(require_admin),
    ):
        try:
            kommo_service.save_kommo_excluded_extensions(body.excluded_extensions)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/admin/kommo-users")
    async def admin_list_kommo_users(_admin: dict = Depends(require_admin)):
        try:
            return await kommo_service.list_admin_kommo_users(jwt_secret=jwt_secret)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            print(f"[kommo] list users failed: {exc}", flush=True)
            raise HTTPException(500, f"Failed to load Kommo users: {exc}") from exc

    @app.post("/api/admin/kommo-extension-user")
    async def admin_set_kommo_extension_user(
        body: KommoExtensionUserRequest,
        _admin: dict = Depends(require_admin),
    ):
        try:
            kommo_service.save_kommo_extension_user(
                extension=body.extension,
                kommo_user_id=body.kommo_user_id,
                kommo_user_name=body.kommo_user_name,
            )
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/api/admin/kommo/oauth/start")
    async def admin_kommo_oauth_start(request: Request, _admin: dict = Depends(require_admin)):
        try:
            return kommo_service.start_oauth(
                jwt_secret=jwt_secret,
                default_redirect_uri=kommo_default_redirect_uri(request),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            print(f"[kommo] oauth start failed: {exc}", flush=True)
            raise HTTPException(500, f"OAuth start failed: {exc}") from exc

    @app.post("/api/admin/kommo/disconnect")
    async def admin_kommo_disconnect(_admin: dict = Depends(require_admin)):
        import permissions_db

        permissions_db.clear_kommo_oauth_tokens()
        return {"ok": True}

    @app.get("/api/admin/kommo/storage")
    async def admin_kommo_storage(_admin: dict = Depends(require_admin)):
        return kommo_service.get_kommo_storage_debug(jwt_secret=jwt_secret)

    @app.post("/api/admin/kommo/test")
    async def admin_kommo_test(
        body: KommoTestRequest | None = None,
        _admin: dict = Depends(require_admin),
    ):
        override = (body.access_token if body else None) or None
        return await kommo_service.admin_test_kommo(
            jwt_secret=jwt_secret,
            override_access_token=override,
        )

    @app.get("/oauth/kommo/callback")
    async def kommo_oauth_callback(
        request: Request,
        code: str = Query(""),
        referer: str = Query(""),
        referrer: str = Query(""),
        state: str = Query(""),
        error: str = Query(""),
        error_description: str = Query(""),
    ):
        oauth_referer = (referer or referrer or request.query_params.get("referer") or request.query_params.get("referrer") or "").strip()
        if error:
            return templates.TemplateResponse(
                request,
                "kommo_oauth_result.html",
                context=html_context(
                    request,
                    success=False,
                    title="Kommo authorization failed",
                    message=error_description or error,
                ),
                status_code=400,
            )
        try:
            result = await kommo_service.complete_oauth_callback(
                code=code.strip(),
                referer=oauth_referer,
                state=state.strip(),
                jwt_secret=jwt_secret,
                default_redirect_uri=kommo_default_redirect_uri(request),
            )
            if result.get("probe_ok", True):
                return templates.TemplateResponse(
                    request,
                    "kommo_oauth_result.html",
                    context=html_context(
                        request,
                        success=True,
                        warn=False,
                        title="Kommo authorized",
                        message=(
                            f"Connected to {result.get('subdomain') or 'Kommo'}. "
                            "Account API verified — softphones can use shared Kommo auth. "
                            "You can close this tab and return to the admin panel."
                        ),
                    ),
                )
            return templates.TemplateResponse(
                request,
                "kommo_oauth_result.html",
                context=html_context(
                    request,
                    success=False,
                    warn=False,
                    title="Kommo authorization incomplete",
                    message=(
                        f"OAuth tokens for {result.get('subdomain') or 'Kommo'} were saved, "
                        "but Kommo rejected CRM API access (GET /api/v4/account → 401). "
                        "Softphones will not receive a working session until this is fixed. "
                        + (result.get("message") or "")
                        + f" Gateway Kommo code: {kommo_service.KOMMO_IMPL_VERSION}."
                    ),
                ),
                status_code=400,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "kommo_oauth_result.html",
                context=html_context(
                    request,
                    success=False,
                    title="Kommo authorization failed",
                    message=str(exc),
                ),
                status_code=400,
            )

    @app.get("/api/kommo/status")
    async def client_kommo_status(user: dict = Depends(require_jwt)):
        ext = _jwt_extension(user)
        return await kommo_service.get_client_status_async(jwt_secret=jwt_secret, extension=ext)

    @app.get("/api/kommo/session")
    async def client_kommo_session(
        user: dict = Depends(require_jwt),
        force_refresh: bool = Query(False),
    ):
        ext = _jwt_extension(user)
        try:
            return await kommo_service.get_client_session(
                jwt_secret=jwt_secret,
                force_refresh=force_refresh,
                extension=ext,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"Kommo session error: {exc}") from exc
