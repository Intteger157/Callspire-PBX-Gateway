"""Kommo integration routes for Callspire PBX Gateway."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

import kommo_call_worker
import kommo_cdr_entity_worker
import kommo_jobs_db
import kommo_service
import kommo_store
import permissions_db
from kommo_crm import KommoCrmClient
from kommo_oauth import (
    account_base_url_from_referer,
    build_gateway_api_web_base_url,
    build_softphone_api_web_base_url,
    normalize_domain_field,
)

log = logging.getLogger("app_kommo")

_workers_started = False
_cfg: dict = {}
_require_jwt: Optional[Callable] = None
_require_admin: Optional[Callable] = None
_query_cdr_fn: Optional[Callable] = None
_download_recording_fn: Optional[Callable] = None
_extension_from_token_fn: Optional[Callable] = None
_verify_linkedid_fn: Optional[Callable] = None

UPLOAD_DIR = Path(__file__).resolve().parent / "kommo_client_uploads"


class ProcessCallBody(BaseModel):
    phone: str
    call_time: str
    session_id: Optional[str] = None
    is_incoming: bool = False
    duration_seconds: int = 0
    was_answered: bool = False
    lead_id: Optional[int] = None
    client_recording_enabled: bool = False
    connection_slot: str = "main"
    call_from_label: Optional[str] = None
    call_log: Optional[str] = None
    enable_recording_upload: bool = True
    answer_time: Optional[str] = None
    call_end_time: Optional[str] = None
    did: Optional[str] = None


class ApplyInboundEntitiesBody(BaseModel):
    force: bool = False
    hours: int = Field(72, ge=1, le=168)


class RetryProcessCallBody(BaseModel):
    phone: str
    call_time: str
    session_id: Optional[str] = None
    is_incoming: bool = False
    duration_seconds: int = 0
    was_answered: bool = False
    lead_id: Optional[int] = None
    client_recording_enabled: bool = False
    connection_slot: str = "main"
    call_from_label: Optional[str] = None
    enable_recording_upload: bool = True
    answer_time: Optional[str] = None
    call_end_time: Optional[str] = None
    did: Optional[str] = None
    job_id: Optional[str] = None


class ReuploadCallJobBody(BaseModel):
    lead_id: Optional[int] = None


def _extension_from_user(user: dict) -> str:
    if _extension_from_token_fn:
        ext = _extension_from_token_fn(user)
        if ext:
            return ext
    return (user.get("extension") or user.get("mikopbx_extension") or "").strip()


async def _get_kommo_session_for_extension(
    extension: str, *, force_refresh: bool = False
) -> Optional[dict[str, Any]]:
    ext = (extension or "").strip()
    try:
        return await kommo_service.get_client_session(
            jwt_secret=_cfg["jwt_secret"],
            force_refresh=force_refresh,
            extension=ext or None,
        )
    except ValueError as exc:
        log.warning("Kommo session unavailable for ext=%s: %s", ext, exc)
        return None
    except Exception as exc:
        log.exception("Kommo session error for ext=%s: %s", ext, exc)
        return None


async def _kommo_crm_client_for_extension(
    extension: str,
) -> tuple[Optional[KommoCrmClient], Optional[str]]:
    ext = (extension or "").strip()
    if not ext:
        return None, "Extension is required"
    session = await _get_kommo_session_for_extension(ext)
    if not session:
        return None, "Kommo session unavailable for this extension"

    async def refresh_token() -> tuple[str, Optional[str]]:
        refreshed = await _get_kommo_session_for_extension(ext, force_refresh=True)
        if refreshed and refreshed.get("access_token"):
            return refreshed["access_token"], refreshed.get("expires_at")
        return session["access_token"], session.get("expires_at")

    client = KommoCrmClient(
        session.get("subdomain") or "",
        session["access_token"],
        account_base_url=session.get("account_base_url"),
        acting_user_id=session.get("kommo_user_id"),
        token_refresher=refresh_token,
    )
    return client, None


async def _fetch_kommo_status_for_extension(extension: str) -> dict[str, Any]:
    """Status for web/desktop clients — reads OAuth from permissions_db via kommo_service."""
    ext = (extension or "").strip()
    status = await kommo_service.get_client_status_async(
        jwt_secret=_cfg["jwt_secret"],
        extension=ext or None,
    )
    mapped = permissions_db.get_kommo_extension_user(ext) if ext else None
    kommo_user_id = (mapped or {}).get("kommo_user_id")
    kommo_user_name = (mapped or {}).get("kommo_user_name") or ""
    excluded = bool(status.get("excluded"))
    status["kommo_user_id"] = kommo_user_id
    status["kommo_user_name"] = kommo_user_name
    status["upload_enabled"] = bool(kommo_user_id) and not excluded
    return status


def _job_to_api(job: dict[str, Any]) -> dict[str, Any]:
    upload_source = job.get("upload_source")
    amo_source = None
    if upload_source == "miko_pbx":
        amo_source = "miko_pbx"
    elif upload_source == "client":
        amo_source = "local"
    return {
        "id": job["id"],
        "status": job["status"],
        "lead_id": job.get("lead_id"),
        "contact_id": job.get("contact_id"),
        "crm_entity": job.get("crm_entity"),
        "upload_source": amo_source,
        "reason": job.get("reason"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


def _kommo_web_base_url() -> str:
    """Web UI base for Kommo/AmoCRM lead links (not /api/v4)."""
    if hasattr(permissions_db, "get_kommo_oauth_tokens"):
        try:
            tokens = permissions_db.get_kommo_oauth_tokens() or {}
            base = (tokens.get("account_base_url") or "").strip().rstrip("/")
            if base.endswith("/api/v4"):
                base = base[: -len("/api/v4")]
            if not base:
                referer = (tokens.get("referer") or "").strip()
                base = account_base_url_from_referer(referer)
            if base:
                return base.rstrip("/")
        except Exception:
            pass
    if hasattr(permissions_db, "get_kommo_integration"):
        try:
            cfg = permissions_db.get_kommo_integration() or {}
            domain = normalize_domain_field(
                (cfg.get("subdomain") or cfg.get("domain") or "").strip()
            )
            if domain:
                url = build_gateway_api_web_base_url(domain) or build_softphone_api_web_base_url(
                    domain
                )
                if url:
                    return url.rstrip("/")
        except Exception:
            pass
    subdomain = (kommo_store.get_subdomain() or "").strip()
    if subdomain:
        return kommo_store.build_account_base_url(subdomain).rstrip("/")
    return ""


def _coerce_int_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _resolve_job_crm_ids(job: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    payload = job.get("payload") or {}
    lead_id = _coerce_int_id(job.get("lead_id")) or _coerce_int_id(payload.get("lead_id"))
    contact_id = _coerce_int_id(job.get("contact_id")) or _coerce_int_id(
        payload.get("contact_id")
    )
    if not lead_id:
        lead_id = kommo_jobs_db.get_job_recording_lead_id(job.get("id") or "")
    return lead_id, contact_id


def _resolve_call_from_label(
    payload: dict[str, Any],
    *,
    reason: Optional[str] = None,
) -> Optional[str]:
    """Outbound CallerID shown in Kommo notes (from client or PBX CDR)."""
    label = (payload.get("call_from_label") or "").strip()
    if label:
        return label
    text = (reason or "").strip()
    if not text:
        return None
    match = re.search(r"Call from ([+\d][\d\s\-()]*)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _job_recording_flags(job: dict[str, Any]) -> tuple[bool, bool]:
    """Return (recording_attached, note_only)."""
    status = job.get("status") or ""
    upload_source = job.get("upload_source") or ""
    payload = job.get("payload") or {}
    was_answered = bool(payload.get("was_answered"))
    recording_attached = status == "uploaded" and upload_source in ("miko_pbx", "client")
    note_only = (
        status == "uploaded"
        and not upload_source
        and was_answered
        and bool(payload.get("enable_recording_upload", True))
    )
    return recording_attached, note_only


def _job_can_retry(job: dict[str, Any]) -> bool:
    status = job.get("status") or ""
    _, note_only = _job_recording_flags(job)
    if status in ("queued", "processing", "waiting_recording"):
        return True
    if status == "failed":
        return True
    if status == "uploaded" and note_only:
        return True
    return False


def _job_crm_link(job: dict[str, Any], kommo_base: str) -> tuple[Optional[str], Optional[str]]:
    lead_id, contact_id = _resolve_job_crm_ids(job)
    crm_entity = (job.get("crm_entity") or "").strip().lower()
    if not kommo_base:
        return None, None
    if lead_id and crm_entity != "contact":
        return f"{kommo_base}/leads/detail/{lead_id}", f"Lead #{lead_id}"
    if contact_id:
        return f"{kommo_base}/contacts/detail/{contact_id}", f"Contact #{contact_id}"
    if lead_id:
        return f"{kommo_base}/leads/detail/{lead_id}", f"Lead #{lead_id}"
    return None, None


def _job_to_admin_api(
    job: dict[str, Any],
    *,
    kommo_user_name: Optional[str] = None,
    kommo_base: str = "",
) -> dict[str, Any]:
    payload = job.get("payload") or {}
    recording_attached, note_only = _job_recording_flags(job)
    lead_id, contact_id = _resolve_job_crm_ids(job)
    crm_url, crm_label = _job_crm_link(job, kommo_base)
    upload_source = job.get("upload_source")
    amo_source = None
    if upload_source == "miko_pbx":
        amo_source = "miko_pbx"
    elif upload_source == "client":
        amo_source = "local"
    pbx_billsec = payload.get("pbx_billsec")
    client_dur = int(payload.get("client_duration_seconds") or payload.get("duration_seconds") or 0)
    display_dur = int(pbx_billsec) if pbx_billsec is not None else client_dur
    reason = job.get("reason")
    call_from_label = _resolve_call_from_label(payload, reason=reason)
    return {
        "id": job["id"],
        "extension": job.get("extension"),
        "kommo_user_name": kommo_user_name or "",
        "phone": payload.get("phone"),
        "call_from_label": call_from_label,
        "call_time": payload.get("call_time"),
        "is_incoming": bool(payload.get("is_incoming")),
        "duration_seconds": display_dur,
        "client_duration_seconds": client_dur if payload.get("client_duration_seconds") else None,
        "pbx_billsec": int(pbx_billsec) if pbx_billsec is not None else None,
        "was_answered": bool(payload.get("was_answered")),
        "status": job.get("status"),
        "lead_id": lead_id,
        "contact_id": contact_id,
        "crm_entity": job.get("crm_entity"),
        "crm_url": crm_url,
        "crm_label": crm_label,
        "upload_source": amo_source,
        "recording_attached": recording_attached,
        "note_only": note_only,
        "can_retry": _job_can_retry(job),
        "can_reupload": True,
        "reason": reason,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "session_id": payload.get("session_id"),
        "pbx_linkedid": payload.get("pbx_linkedid"),
    }


def _pbx_local_window_hours(cfg: dict[str, Any], hours: int) -> tuple[str, str]:
    offset = float(cfg.get("pbx_utc_offset_hours") or 0)
    now_utc = datetime.now(timezone.utc)
    end_local = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
    start_local = end_local - timedelta(hours=max(1, hours))
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_local.strftime(fmt), end_local.strftime(fmt)


def _pbx_start_to_iso(cfg: dict[str, Any], start: str) -> str:
    text = (start or "").strip()
    if not text:
        return ""
    offset = float(cfg.get("pbx_utc_offset_hours") or 0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.strptime(text[:19], fmt)
            utc = local.replace(tzinfo=timezone.utc) - timedelta(hours=offset)
            return utc.isoformat()
        except ValueError:
            continue
    return text


def _inbound_crm_links(
    kommo_base: str,
    *,
    lead_id: Optional[int],
    contact_id: Optional[int],
    task_id: Optional[int],
    task_created: bool,
) -> dict[str, Optional[str]]:
    base = (kommo_base or "").rstrip("/")
    crm_url = crm_label = task_url = task_label = None
    lid = int(lead_id) if lead_id else None
    cid = int(contact_id) if contact_id else None
    tid = int(task_id) if task_id else None
    if lid and base:
        crm_url = f"{base}/leads/detail/{lid}"
        crm_label = f"Lead #{lid}"
    elif cid and base:
        crm_url = f"{base}/contacts/detail/{cid}"
        crm_label = f"Contact #{cid}"
    if task_created and base:
        if tid and tid > 0:
            task_url = f"{base}/leads/detail/{lid}" if lid else (
                f"{base}/contacts/detail/{cid}" if cid else None
            )
            task_label = f"Task #{tid}"
        elif crm_url:
            task_url = crm_url
            task_label = "Task"
    return {
        "crm_url": crm_url,
        "crm_label": crm_label,
        "task_url": task_url,
        "task_label": task_label,
    }


def _inbound_call_to_admin_api(
    call: dict[str, Any],
    *,
    entity: Optional[dict[str, Any]],
    job: Optional[dict[str, Any]],
    kommo_base: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    payload = (job or {}).get("payload") or {}
    entity = entity or {}
    job_api: Optional[dict[str, Any]] = None
    if job:
        mapped = permissions_db.get_kommo_extension_user(job.get("extension") or "")
        kommo_name = (mapped or {}).get("kommo_user_name") or ""
        job_api = _job_to_admin_api(job, kommo_user_name=kommo_name, kommo_base=kommo_base)

    was_answered = bool(call.get("was_answered"))
    if job_api and job_api.get("was_answered"):
        was_answered = True

    lead_id = entity.get("lead_id") or (job_api or {}).get("lead_id")
    contact_id = entity.get("contact_id") or (job_api or {}).get("contact_id")
    task_created = bool(entity.get("task_created"))
    task_id = entity.get("task_id")
    links = _inbound_crm_links(
        kommo_base,
        lead_id=lead_id,
        contact_id=contact_id,
        task_id=task_id,
        task_created=task_created,
    )

    dur = int(call.get("billsec") or call.get("duration") or 0)
    if job_api and job_api.get("duration_seconds"):
        dur = max(dur, int(job_api["duration_seconds"]))

    ext = (call.get("ext") or "").strip() or (job_api or {}).get("extension") or ""

    has_entity_outcome = bool(
        lead_id or contact_id or (task_created and task_id)
    )
    can_apply_entities = not was_answered and not has_entity_outcome

    return {
        "id": call.get("linkedid") or "",
        "linkedid": call.get("linkedid") or "",
        "call_time": _pbx_start_to_iso(cfg, call.get("start") or "")
        or (job_api or {}).get("call_time"),
        "extension": ext,
        "kommo_user_name": (job_api or {}).get("kommo_user_name") or "",
        "phone": call.get("phone") or entity.get("phone") or payload.get("phone"),
        "is_incoming": True,
        "was_answered": was_answered,
        "call_status": "answered" if was_answered else "missed",
        "duration_seconds": dur,
        "task_created": task_created,
        "task_id": int(task_id) if task_id else None,
        "lead_id": int(lead_id) if lead_id else None,
        "contact_id": int(contact_id) if contact_id else None,
        "crm_url": links["crm_url"] or (job_api or {}).get("crm_url"),
        "crm_label": links["crm_label"] or (job_api or {}).get("crm_label"),
        "task_url": links["task_url"],
        "task_label": links["task_label"],
        "job_id": job.get("id") if job else None,
        "upload_status": job.get("status") if job else None,
        "status": job.get("status") if job else None,
        "recording_attached": (job_api or {}).get("recording_attached"),
        "note_only": (job_api or {}).get("note_only"),
        "can_retry": bool((job_api or {}).get("can_retry")),
        "can_reupload": bool(job),
        "can_apply_entities": can_apply_entities,
        "entity_rule_id": entity.get("rule_id"),
        "reason": (job_api or {}).get("reason"),
        "source": "pbx_cdr",
    }


async def _ensure_workers() -> None:
    global _workers_started
    if _workers_started:
        return
    if not _query_cdr_fn or not _download_recording_fn:
        msg = (
            "[kommo] workers NOT started — register_kommo_routes missing "
            "query_cdr/download_recording (see APP_PY_PATCH.md)"
        )
        log.warning(msg)
        print(msg, flush=True)
        return

    async def query_cdr(**kwargs):
        return await _query_cdr_fn(**kwargs)

    async def download_recording(linked_id: str, dest: Path) -> bool:
        return await _download_recording_fn(linked_id, dest)

    await kommo_call_worker.start_workers(
        get_session_for_extension=_get_kommo_session_for_extension,
        query_cdr=query_cdr,
        download_recording=download_recording,
        verify_linkedid=_verify_linkedid_fn,
    )
    await kommo_cdr_entity_worker.start_worker(
        cfg=_cfg,
        get_session_for_extension=_get_kommo_session_for_extension,
        query_cdr=query_cdr,
    )
    _workers_started = True


async def ensure_kommo_workers_started() -> None:
    """Start Kommo upload workers (also invoked from app lifespan on service boot)."""
    await _ensure_workers()


def register_kommo_routes(
    app,
    *,
    cfg: dict,
    require_admin,
    require_jwt,
    templates,
    html_context,
    kommo_default_redirect_uri,
    query_cdr=None,
    download_recording=None,
    extension_from_token=None,
    verify_linkedid=None,
    **kwargs,
) -> None:
    if kwargs:
        log.info(
            "register_kommo_routes: ignoring legacy kwargs %s",
            sorted(kwargs.keys()),
        )
    global _cfg, _require_jwt, _require_admin, _query_cdr_fn, _download_recording_fn, _extension_from_token_fn, _verify_linkedid_fn
    _cfg = cfg
    kommo_call_worker.configure(cfg)
    kommo_cdr_entity_worker.configure(cfg)
    _require_jwt = require_jwt
    _require_admin = require_admin
    _query_cdr_fn = query_cdr
    _download_recording_fn = download_recording
    _extension_from_token_fn = extension_from_token
    _verify_linkedid_fn = verify_linkedid

    db_path = cfg.get("permissions_db_path") or cfg.get("kommo_jobs_db_path")
    kommo_store.init_db(db_path)
    kommo_jobs_db.init_db(db_path)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    import kommo_recording
    kommo_recording.configure_pbx_timezone(cfg.get("pbx_utc_offset_hours", 3))

    router = APIRouter()

    @router.get("/api/kommo/status")
    async def kommo_status(user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        return await _fetch_kommo_status_for_extension(ext)

    @router.get("/api/kommo/session")
    async def kommo_session(
        user: dict = Depends(require_jwt),
        force_refresh: bool = Query(False),
    ):
        ext = _extension_from_user(user)
        status = await _fetch_kommo_status_for_extension(ext)
        if status.get("excluded"):
            raise HTTPException(403, "Extension excluded from gateway Kommo")
        if not status.get("offer_gateway"):
            raise HTTPException(503, "Kommo gateway module is not available")
        session = await _get_kommo_session_for_extension(ext, force_refresh=force_refresh)
        if not session:
            raise HTTPException(503, "Kommo session unavailable")
        return session

    @router.get("/api/kommo/contact")
    async def kommo_contact(
        user: dict = Depends(require_jwt),
        phone: str = Query(""),
    ):
        ext = _extension_from_user(user)
        status = await _fetch_kommo_status_for_extension(ext)
        if not status.get("offer_gateway"):
            raise HTTPException(503, "Kommo gateway module is not available")
        session = await _get_kommo_session_for_extension(ext)
        if not session:
            raise HTTPException(503, "Kommo session unavailable")
        clean = (phone or "").strip()
        if not clean:
            raise HTTPException(400, "phone is required")

        async def refresh_token() -> tuple[str, Optional[str]]:
            refreshed = await _get_kommo_session_for_extension(ext, force_refresh=True)
            if refreshed and refreshed.get("access_token"):
                return refreshed["access_token"], refreshed.get("expires_at")
            return session["access_token"], session.get("expires_at")

        client = KommoCrmClient(
            session.get("subdomain") or "",
            session["access_token"],
            account_base_url=session.get("account_base_url"),
            acting_user_id=session.get("kommo_user_id"),
            token_refresher=refresh_token,
        )
        try:
            return await client.lookup_contact_display(clean)
        finally:
            await client.close()

    @router.get("/api/kommo/call-attachments")
    async def kommo_call_attachments(
        user: dict = Depends(require_jwt),
        hours: int = Query(72),
    ):
        ext = _extension_from_user(user)
        if not ext:
            raise HTTPException(403, "No PBX extension on this account")
        items = kommo_jobs_db.list_uploaded_jobs_for_extension(ext, hours=hours)
        return {"items": items}

    @router.post("/api/kommo/process-call")
    async def kommo_process_call(body: ProcessCallBody, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        if not ext:
            raise HTTPException(403, "No PBX extension on this account")
        status = await _fetch_kommo_status_for_extension(ext)
        if not status.get("offer_gateway"):
            raise HTTPException(503, "Kommo gateway module is not available")
        if not status.get("upload_enabled"):
            raise HTTPException(
                403,
                "Kommo upload is not enabled for this extension (map a Kommo user in admin)",
            )

        dedup = kommo_call_worker.build_dedup_key(
            ext, body.phone, body.call_time, body.session_id
        )
        existing = kommo_jobs_db.get_job_by_dedup(dedup)
        if existing and existing["status"] == "uploaded":
            return _job_to_api(existing)

        overlap = kommo_jobs_db.find_overlapping_active_job(
            ext, body.phone, body.call_time, session_id=body.session_id
        )
        if overlap and overlap.get("id"):
            overlap_status = str(overlap.get("status") or "")
            if overlap_status == "uploaded":
                print(
                    f"[kommo] reusing uploaded job {overlap['id']} for {body.phone}",
                    flush=True,
                )
                return _job_to_api(overlap)
            if overlap_status in ("failed", "skipped"):
                revived = kommo_jobs_db.requeue_job_for_overlap_resubmit(
                    overlap["id"], body.model_dump()
                )
                if revived:
                    print(
                        f"[kommo] revived failed job {overlap['id']} for {body.phone} "
                        f"(was {overlap_status})",
                        flush=True,
                    )
                    await _ensure_workers()
                    return _job_to_api(revived)
            print(
                f"[kommo] reusing overlapping job {overlap['id']} for {body.phone} "
                f"(status={overlap_status})",
                flush=True,
            )
            return _job_to_api(overlap)

        payload = body.model_dump()
        initial_status = "waiting_recording" if body.client_recording_enabled else "queued"
        job = kommo_jobs_db.create_job(ext, dedup, payload, status=initial_status)
        if job.get("id"):
            dropped = kommo_jobs_db.fail_superseded_queued_jobs(
                ext,
                body.phone,
                except_job_id=job["id"],
                call_time=body.call_time,
                session_id=body.session_id,
            )
            if dropped:
                print(
                    f"[kommo] dropped {dropped} superseded queued job(s) for {body.phone}",
                    flush=True,
                )
            weaker = kommo_jobs_db.fail_weaker_duplicate_jobs(
                ext,
                body.phone,
                body.call_time,
                job["id"],
                incoming_was_answered=bool(body.was_answered),
            )
            if weaker:
                print(
                    f"[kommo] dropped {weaker} weaker duplicate job(s) for {body.phone}",
                    flush=True,
                )
        await _ensure_workers()
        return _job_to_api(job)

    @router.get("/api/kommo/process-call/{job_id}")
    async def kommo_process_call_status(job_id: str, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["extension"] != ext and user.get("role") != "admin":
            raise HTTPException(403, "Forbidden")
        return _job_to_api(job)

    @router.put("/api/kommo/process-call/{job_id}/recording")
    async def kommo_process_call_recording(
        job_id: str,
        user: dict = Depends(require_jwt),
        file: UploadFile = File(...),
    ):
        ext = _extension_from_user(user)
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["extension"] != ext:
            raise HTTPException(403, "Forbidden")

        dest_dir = UPLOAD_DIR / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "recording.wav").suffix or ".wav"
        dest = dest_dir / f"client{suffix}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)

        kommo_jobs_db.update_job(
            job_id,
            recording_path=str(dest),
            upload_source="client",
            status="queued",
        )
        await _ensure_workers()
        return {"ok": True, "path": str(dest)}

    @router.post("/api/kommo/process-call/retry")
    async def kommo_process_call_retry(body: RetryProcessCallBody, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        if not ext:
            raise HTTPException(403, "No PBX extension")
        if body.job_id:
            job = kommo_jobs_db.get_job(body.job_id)
            if job and job["extension"] == ext:
                refreshed = kommo_jobs_db.reset_job_for_retry(body.job_id)
                if refreshed and body.lead_id:
                    payload = dict(refreshed.get("payload") or {})
                    payload["lead_id"] = body.lead_id
                    refreshed = kommo_jobs_db.update_job(
                        body.job_id, payload_json=payload
                    )
                await _ensure_workers()
                return _job_to_api(refreshed or job)

        req = ProcessCallBody(**body.model_dump(exclude={"job_id"}))
        return await kommo_process_call(req, user)

    @router.get("/oauth/kommo/callback")
    async def kommo_oauth_callback(
        request: Request,
        code: str = Query(""),
        state: str = Query(""),
        referer: str = Query(""),
    ):
        if not code:
            raise HTTPException(400, "Missing code")
        try:
            await kommo_service.complete_oauth_callback(
                code=code,
                referer=referer,
                state=state,
                jwt_secret=_cfg["jwt_secret"],
                default_redirect_uri=kommo_default_redirect_uri(request),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("kommo oauth callback failed")
            raise HTTPException(502, str(exc)) from exc
        ctx = html_context(request)
        base = ctx.get("base_path") or ""
        return RedirectResponse(url=f"{base}/admin?kommo_authorized=1", status_code=302)

    @router.get("/admin/kommo", response_class=HTMLResponse)
    async def admin_kommo_page(request: Request, _admin: dict = Depends(require_admin)):
        status = await _fetch_kommo_status_for_extension("")
        cfg = permissions_db.get_kommo_integration()
        ctx = html_context(request)
        ctx.update({"kommo": status, "subdomain": cfg.get("subdomain") or ""})
        return templates.TemplateResponse(request, "admin_kommo.html", context=ctx)

    @router.get("/api/admin/kommo/call-jobs")
    async def admin_kommo_call_jobs(
        _admin: dict = Depends(require_admin),
        hours: int = Query(72, ge=1, le=168),
        extension: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        phone: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        kommo_base = _kommo_web_base_url()
        total = kommo_jobs_db.count_call_jobs_admin(
            hours=hours,
            extension=extension,
            status=status,
            phone=phone,
        )
        rows = kommo_jobs_db.list_call_jobs_admin(
            hours=hours,
            extension=extension,
            status=status,
            phone=phone,
            limit=limit,
            offset=offset,
        )
        items = []
        for job in rows:
            mapped = permissions_db.get_kommo_extension_user(job.get("extension") or "")
            kommo_name = (mapped or {}).get("kommo_user_name") or ""
            items.append(
                _job_to_admin_api(job, kommo_user_name=kommo_name, kommo_base=kommo_base)
            )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "kommo_account_url": kommo_base,
        }

    @router.get("/api/admin/kommo/inbound-call-log")
    async def admin_inbound_call_log(
        _admin: dict = Depends(require_admin),
        hours: int = Query(72, ge=1, le=168),
        extension: Optional[str] = Query(None),
        call_status: Optional[str] = Query(None, description="missed or answered"),
        phone: Optional[str] = Query(None),
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        if not _query_cdr_fn:
            raise HTTPException(503, "CDR query not configured on gateway")
        kommo_base = _kommo_web_base_url()
        start_from, start_to = _pbx_local_window_hours(_cfg, hours)
        rows = await _query_cdr_fn(
            start_from=start_from,
            start_to=start_to,
            limit=5000,
            offset=0,
        )
        exts = kommo_cdr_entity_worker.known_extensions(_cfg)
        calls = kommo_cdr_entity_worker.extract_inbound_cdr_calls(rows, exts)
        entity_map = kommo_jobs_db.map_cdr_entity_processed(hours=hours)
        job_map = kommo_jobs_db.map_incoming_jobs_by_linkedid(hours=hours)

        items: list[dict[str, Any]] = []
        for call in calls:
            lid = (call.get("linkedid") or "").strip()
            if not lid:
                continue
            ext = (call.get("ext") or "").strip()
            if extension and ext != extension.strip():
                continue
            ph = (call.get("phone") or "").strip()
            if phone and phone.strip() not in ph:
                continue
            entity = entity_map.get(lid)
            job = job_map.get(lid)
            item = _inbound_call_to_admin_api(
                call,
                entity=entity,
                job=job,
                kommo_base=kommo_base,
                cfg=_cfg,
            )
            st = (call_status or "").strip().lower()
            if st in ("missed", "answered") and item.get("call_status") != st:
                continue
            items.append(item)

        total = len(items)
        items = items[offset : offset + limit]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "kommo_account_url": kommo_base,
        }

    @router.post("/api/admin/kommo/inbound-call-log/{linkedid}/apply-entities")
    async def admin_apply_inbound_entities(
        linkedid: str,
        body: ApplyInboundEntitiesBody,
        _admin: dict = Depends(require_admin),
    ):
        lid = (linkedid or "").strip()
        if not lid:
            raise HTTPException(400, "linkedid is required")
        result = await kommo_cdr_entity_worker.apply_missed_inbound_entities_manual(
            lid,
            hours=body.hours,
            force=body.force,
        )
        reason = (result.get("reason") or "").strip()
        if reason == "call_not_found_or_not_missed_inbound":
            raise HTTPException(404, reason)
        return result

    @router.post("/api/admin/kommo/call-jobs/{job_id}/retry")
    async def admin_kommo_call_job_retry(
        job_id: str,
        _admin: dict = Depends(require_admin),
    ):
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if not _job_can_retry(job):
            raise HTTPException(
                400,
                f"Job status '{job.get('status')}' cannot be retried from admin",
            )
        refreshed = kommo_jobs_db.reset_job_for_retry(job_id)
        if not refreshed:
            raise HTTPException(500, "Failed to requeue job")
        await _ensure_workers()
        kommo_base = _kommo_web_base_url()
        mapped = permissions_db.get_kommo_extension_user(refreshed.get("extension") or "")
        kommo_name = (mapped or {}).get("kommo_user_name") or ""
        return _job_to_admin_api(
            refreshed, kommo_user_name=kommo_name, kommo_base=kommo_base
        )

    @router.get("/api/admin/kommo/leads-for-phone")
    async def admin_kommo_leads_for_phone(
        _admin: dict = Depends(require_admin),
        phone: str = Query(""),
        extension: str = Query(""),
    ):
        clean_phone = (phone or "").strip()
        ext = (extension or "").strip()
        if not clean_phone:
            raise HTTPException(400, "phone is required")
        if not ext:
            raise HTTPException(400, "extension is required")
        client, err = await _kommo_crm_client_for_extension(ext)
        if not client:
            raise HTTPException(503, err or "Kommo session unavailable")
        try:
            items = await client.list_open_leads_for_phone(clean_phone)
            return {"items": items}
        finally:
            await client.close()

    @router.post("/api/admin/kommo/call-jobs/{job_id}/reupload")
    async def admin_kommo_call_job_reupload(
        job_id: str,
        body: ReuploadCallJobBody,
        _admin: dict = Depends(require_admin),
    ):
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        lead_id = _coerce_int_id(body.lead_id)
        refreshed = kommo_jobs_db.reset_job_for_reupload(job_id, lead_id=lead_id)
        if not refreshed:
            raise HTTPException(500, "Failed to requeue job for re-upload")
        await _ensure_workers()
        kommo_base = _kommo_web_base_url()
        mapped = permissions_db.get_kommo_extension_user(refreshed.get("extension") or "")
        kommo_name = (mapped or {}).get("kommo_user_name") or ""
        return _job_to_admin_api(
            refreshed, kommo_user_name=kommo_name, kommo_base=kommo_base
        )

    app.include_router(router)

    @app.on_event("startup")
    async def _kommo_startup():
        asyncio.create_task(_ensure_workers())
