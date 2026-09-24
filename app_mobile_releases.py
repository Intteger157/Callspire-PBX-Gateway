"""Mobile app update proxy — GitHub private releases → authenticated clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import mobile_releases_github as gh
import mobile_releases_store as store

log = logging.getLogger("mobile_releases")

_cfg: dict = {}
_require_jwt: Callable = None
_require_admin: Callable = None
_jwt_secret: str = ""
_public_base_url: Callable[[Request], str] | None = None

_DOWNLOAD_TTL_SECONDS = 1800


class MobileReleasesConfigRequest(BaseModel):
    enabled: bool | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    github_token: str | None = None
    asset_name: str = Field(default="IGCaller.apk")


def _public_url(request: Request) -> str:
    if _public_base_url is not None:
        return _public_base_url(request).rstrip("/")
    return str(request.base_url).rstrip("/")


def _download_sig(tag: str, exp: int) -> str:
    msg = f"{tag}:{exp}".encode()
    digest = hmac.new(_jwt_secret.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _issue_download_token(tag: str) -> tuple[str, int]:
    exp = int(time.time()) + _DOWNLOAD_TTL_SECONDS
    sig = _download_sig(tag, exp)
    token = base64.urlsafe_b64encode(f"{tag}:{exp}:{sig}".encode()).decode().rstrip("=")
    return token, exp


def _verify_download_token(tag: str, token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token + "==")
        parts = raw.decode().split(":")
        if len(parts) != 3:
            return False
        tok_tag, exp_str, sig = parts
        if tok_tag != tag:
            return False
        exp = int(exp_str)
        if exp < int(time.time()):
            return False
        expected = _download_sig(tag, exp)
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _release_to_api(release: gh.GithubRelease, request: Request) -> dict[str, Any]:
    dl_token, dl_exp = _issue_download_token(release.tag)
    query = urlencode({"tag": release.tag, "dl": dl_token})
    base = _public_url(request)
    return {
        "platform": "android",
        "package": "com.intermarkmobile",
        "version": release.version,
        "tag": release.tag,
        "name": release.name,
        "publishedAt": release.published_at,
        "releaseNotes": release.body.strip(),
        "assetName": release.asset.name,
        "assetSize": release.asset.size,
        "downloadUrl": f"{base}/api/v1/app/mobile/download?{query}",
        "downloadExpiresAt": dl_exp,
    }


async def _load_latest_release() -> gh.GithubRelease:
    if not store.is_ready():
        raise HTTPException(503, "Mobile release proxy is not configured")
    settings = store.github_settings()
    try:
        return await gh.fetch_latest_release(
            settings["owner"],
            settings["repo"],
            settings["token"],
            settings["asset_name"],
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        log.warning("GitHub release fetch failed: %s", exc)
        raise HTTPException(502, str(exc) or "GitHub release lookup failed") from exc
    except Exception as exc:
        log.exception("GitHub release fetch unexpected error")
        raise HTTPException(502, f"GitHub release lookup failed: {exc}") from exc


def register_mobile_releases_routes(
    app,
    *,
    cfg: dict,
    require_admin,
    require_jwt,
    templates=None,
    html_context=None,
    public_base_url: Callable[[Request], str] | None = None,
) -> None:
    global _cfg, _require_jwt, _require_admin, _jwt_secret, _public_base_url
    _cfg = cfg
    _require_jwt = require_jwt
    _require_admin = require_admin
    _jwt_secret = str(cfg.get("jwt_secret") or "")
    _public_base_url = public_base_url

    db_path = cfg.get("permissions_db_path") or cfg.get("kommo_jobs_db_path")
    store.init_db(db_path)

    if not store.get_config().get("github_owner"):
        owner = (
            (cfg.get("mobile_releases_github_owner") or "")
            or os.environ.get("GITHUB_MOBILE_RELEASES_OWNER", "")
        ).strip()
        repo = (
            (cfg.get("mobile_releases_github_repo") or "")
            or os.environ.get("GITHUB_MOBILE_RELEASES_REPO", "")
        ).strip()
        if owner and repo:
            store.update_config(
                enabled=bool(cfg.get("mobile_releases_enabled", True)),
                github_owner=owner,
                github_repo=repo,
                asset_name=(cfg.get("mobile_releases_asset_name") or "IGCaller.apk"),
            )

    router = APIRouter()

    @router.get("/api/v1/app/mobile/latest")
    async def mobile_latest(request: Request, _user: dict = Depends(require_jwt)):
        release = await _load_latest_release()
        return _release_to_api(release, request)

    @router.get("/api/v1/app/mobile/download")
    async def mobile_download(
        tag: str = Query(...),
        dl: str = Query(..., min_length=8),
    ):
        if not store.is_ready():
            raise HTTPException(503, "Mobile release proxy is not configured")
        clean_tag = tag.strip()
        if not _verify_download_token(clean_tag, dl.strip()):
            raise HTTPException(401, "Invalid or expired download token")

        settings = store.github_settings()
        try:
            release = await gh.fetch_release_by_tag(
                settings["owner"],
                settings["repo"],
                settings["token"],
                clean_tag,
                settings["asset_name"],
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            log.warning("GitHub release by tag failed: %s", exc)
            raise HTTPException(502, "GitHub release lookup failed") from exc

        try:
            response, iterator = await gh.stream_release_asset(
                settings["owner"],
                settings["repo"],
                settings["token"],
                release.asset.asset_id,
            )
        except RuntimeError as exc:
            log.warning("GitHub asset stream failed: %s", exc)
            raise HTTPException(502, "GitHub asset download failed") from exc

        headers = {
            "Content-Disposition": f'attachment; filename="{release.asset.name}"',
        }
        if response.headers.get("content-length"):
            headers["Content-Length"] = response.headers["content-length"]
        media_type = release.asset.content_type or "application/vnd.android.package-archive"
        return StreamingResponse(iterator, media_type=media_type, headers=headers)

    @router.get("/api/admin/mobile-releases/config")
    async def admin_mobile_releases_config(_admin: dict = Depends(require_admin)):
        return store.get_admin_config()

    @router.post("/api/admin/mobile-releases/config")
    async def admin_mobile_releases_config_update(
        body: MobileReleasesConfigRequest,
        _admin: dict = Depends(require_admin),
    ):
        updated = store.update_config(
            enabled=body.enabled,
            github_owner=body.github_owner,
            github_repo=body.github_repo,
            github_token=body.github_token,
            asset_name=body.asset_name,
        )
        return {"success": True, "config": updated}

    @router.get("/api/admin/mobile-releases/test")
    async def admin_mobile_releases_test(_admin: dict = Depends(require_admin)):
        release = await _load_latest_release()
        return {
            "ok": True,
            "tag": release.tag,
            "version": release.version,
            "asset": release.asset.name,
            "size": release.asset.size,
        }

    if html_context is not None:

        @router.get("/admin/mobile-releases")
        async def admin_mobile_releases_page(request: Request):
            """Legacy URL — settings live in the main admin SPA (login required for API)."""
            ctx = html_context(request)
            base = str(ctx.get("base_path") or "").rstrip("/")
            return RedirectResponse(url=f"{base}/admin#mobile-releases", status_code=302)

    app.include_router(router)
