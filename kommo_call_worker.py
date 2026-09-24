"""Background workers for Kommo process-call jobs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import kommo_jobs_db
from kommo_crm import KommoCrmClient, ProcessCallOutcome
from kommo_recording import resolve_pbx_call, MAX_PBX_RECORDING_JOB_RETRIES, JOB_RETRY_WAITS_SEC

log = logging.getLogger("kommo_call_worker")


def _pbx_recording_still_expected(payload: dict[str, Any], cdr: Any) -> bool:
    if not bool(payload.get("enable_recording_upload", True)):
        return False
    if not cdr.was_answered:
        return False
    if cdr.has_recording:
        return True
    return int(cdr.billsec or 0) >= 3


def _apply_pbx_cdr_to_payload(payload: dict[str, Any], cdr: Any) -> tuple[Any, Any, int]:
    """Merge PBX CDR truth into job payload for Kommo upload and desktop status sync."""
    payload["pbx_linkedid"] = cdr.linkedid
    billsec = int(cdr.billsec or 0)
    if billsec <= 0 and cdr.was_answered and int(cdr.duration or 0) > 0:
        billsec = int(cdr.duration)
    payload["pbx_was_answered"] = bool(cdr.was_answered)
    payload["was_answered"] = bool(cdr.was_answered)
    payload["pbx_duration_seconds"] = billsec if cdr.was_answered else 0
    if cdr.was_answered:
        if billsec > 0:
            payload["duration_seconds"] = billsec
    else:
        payload["duration_seconds"] = 0
    if cdr.caller_id and not payload.get("call_from_label"):
        payload["call_from_label"] = cdr.caller_id
    return cdr.kommo_call_result, cdr.kommo_call_status, billsec


WORKER_COUNT = 3
CLIENT_RECORDING_WAIT_SECONDS = 360
RECORDINGS_DIR = Path(__file__).resolve().parent / "kommo_upload_recordings"
# Web + mobile on one extension: web may report queue CANCEL as missed before mobile answers.
INBOUND_UNANSWERED_DEFER_WAITS_SEC = (15, 15, 15)
INBOUND_UNANSWERED_DEFER_MAX = len(INBOUND_UNANSWERED_DEFER_WAITS_SEC)

_running = False
_tasks: list[asyncio.Task] = []
_cfg: dict[str, Any] = {}


def configure(cfg: dict[str, Any] | None = None) -> None:
    """Apply gateway config (called from register_kommo_routes before workers start)."""
    global _cfg, WORKER_COUNT, CLIENT_RECORDING_WAIT_SECONDS
    if not cfg:
        return
    _cfg = cfg
    try:
        WORKER_COUNT = max(1, min(32, int(cfg.get("kommo_worker_count") or WORKER_COUNT)))
    except (TypeError, ValueError):
        pass
    retries = cfg.get("kommo_max_recording_retries")
    if retries is not None:
        try:
            import kommo_recording

            kommo_recording.MAX_PBX_RECORDING_JOB_RETRIES = max(
                1,
                min(20, int(retries)),
            )
        except (TypeError, ValueError):
            pass


def build_dedup_key(extension: str, phone: str, call_time: str, session_id: Optional[str]) -> str:
    sid = session_id or ""
    return f"{extension}_{phone}_{call_time}_{sid}"


def _parse_call_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _sha256_file(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


async def start_workers(
    *,
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
    verify_linkedid: Optional[Callable[..., Awaitable[bool]]] = None,
    **kwargs: Any,
) -> None:
    if kwargs:
        log.info("start_workers: ignoring extra kwargs %s", sorted(kwargs.keys()))
    global _running, _tasks
    if _running:
        return
    _running = True
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    recovered = kommo_jobs_db.recover_orphaned_processing_jobs()
    if recovered:
        msg = f"[kommo_call_worker] Requeued {recovered} orphaned processing job(s)"
        log.info(msg)
        print(msg, flush=True)
    for i in range(WORKER_COUNT):
        _tasks.append(
            asyncio.create_task(
                _worker_loop(
                    i,
                    get_session_for_extension,
                    query_cdr,
                    download_recording,
                    verify_linkedid,
                ),
                name=f"kommo-worker-{i}",
            )
        )
    msg = f"[kommo_call_worker] Started {WORKER_COUNT} Kommo call upload workers"
    log.info(msg)
    print(msg, flush=True)


async def stop_workers() -> None:
    global _running, _tasks
    _running = False
    for t in _tasks:
        t.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []


async def _worker_loop(
    worker_id: int,
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
    verify_linkedid: Optional[Callable[..., Awaitable[bool]]] = None,
) -> None:
    while _running:
        try:
            job = kommo_jobs_db.claim_next_job(["queued", "waiting_recording"])
            if not job:
                await asyncio.sleep(0.5)
                continue
            print(
                f"[kommo_call_worker] worker {worker_id} claimed job {job['id']} "
                f"ext={job.get('extension')}",
                flush=True,
            )
            await _process_job(
                job,
                get_session_for_extension,
                query_cdr,
                download_recording,
                verify_linkedid,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("worker %s error: %s", worker_id, exc)
            print(f"[kommo_call_worker] worker {worker_id} error: {exc}", flush=True)
            await asyncio.sleep(2.0)


async def _process_job(
    job: dict[str, Any],
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
    verify_linkedid: Optional[Callable[..., Awaitable[bool]]] = None,
) -> None:
    job_id = job["id"]
    payload = dict(job["payload"])
    extension = job["extension"]

    session = await get_session_for_extension(extension)
    if not session or not session.get("access_token"):
        kommo_jobs_db.update_job(job_id, status="failed", reason="Kommo session unavailable")
        print(f"[kommo_call_worker] job {job_id} failed: Kommo session unavailable", flush=True)
        return

    async def refresh_token() -> tuple[str, Optional[str]]:
        refreshed = await get_session_for_extension(extension, force_refresh=True)
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

    keep_recording_files = False
    try:
        audio_path: Optional[str] = job.get("recording_path")
        if audio_path and not Path(audio_path).is_file():
            print(
                f"[kommo_call_worker] job {job_id}: recording file missing on disk "
                f"({audio_path}) — will re-fetch from PBX if needed",
                flush=True,
            )
            audio_path = None
            kommo_jobs_db.update_job(job_id, recording_path=None)

        upload_source: Optional[str] = job.get("upload_source")

        client_recording = bool(payload.get("client_recording_enabled"))
        connection_slot = (payload.get("connection_slot") or "main").lower()
        prefer_miko = connection_slot != "secondary" and not client_recording

        if client_recording and not audio_path:
            kommo_jobs_db.update_job(job_id, status="waiting_recording")
            waited = 0
            while waited < CLIENT_RECORDING_WAIT_SECONDS:
                await asyncio.sleep(2.0)
                waited += 2
                refreshed = kommo_jobs_db.get_job(job_id)
                if refreshed and refreshed.get("recording_path"):
                    audio_path = refreshed["recording_path"]
                    break
                if refreshed and refreshed.get("status") == "processing" and refreshed.get("recording_path"):
                    audio_path = refreshed["recording_path"]
                    break
            if not audio_path:
                kommo_jobs_db.update_job(
                    job_id,
                    status="failed",
                    reason="Client recording not received within timeout",
                )
                return

        call_result_override: Optional[str] = None
        call_status_override: Optional[int] = None
        cdr_note_created = False
        pbx_billsec: Optional[int] = None

        if audio_path and Path(audio_path).is_file():
            if not upload_source:
                upload_source = "client" if client_recording else "miko_pbx"
        elif prefer_miko:
            call_time = _parse_call_time(payload.get("call_time") or "")
            answer_time_raw = payload.get("answer_time")
            answer_time = _parse_call_time(answer_time_raw) if answer_time_raw else None
            call_end_raw = payload.get("call_end_time")
            call_end_time = _parse_call_time(call_end_raw) if call_end_raw else None
            used_linkedids = kommo_jobs_db.list_used_pbx_linkedids(
                extension, exclude_job_id=job_id
            )
            if used_linkedids:
                print(
                    f"[kommo_call_worker] job {job_id}: excluding {len(used_linkedids)} "
                    f"already-used PBX linkedid(s)",
                    flush=True,
                )

            def _claim_linkedid(linkedid: str) -> bool:
                ok = kommo_jobs_db.try_claim_pbx_linkedid(
                    linkedid,
                    job_id,
                    extension,
                    phone=(payload.get("phone") or "").strip(),
                    session_id=payload.get("session_id"),
                    call_time=payload.get("call_time"),
                )
                if ok:
                    payload["pbx_linkedid"] = linkedid
                    kommo_jobs_db.update_job(job_id, payload_json=payload)
                    print(
                        f"[kommo_call_worker] job {job_id}: claimed linkedid={linkedid} "
                        f"session={payload.get('session_id') or '-'}",
                        flush=True,
                    )
                return ok

            def _release_linkedid(linkedid: str) -> None:
                if kommo_jobs_db.release_pbx_linkedid_claim(linkedid, job_id):
                    print(
                        f"[kommo_call_worker] job {job_id}: released linkedid={linkedid}",
                        flush=True,
                    )

            print(
                f"[kommo_call_worker] job {job_id}: resolving PBX CDR "
                f"phone={payload.get('phone')} ext={extension} session={payload.get('session_id') or '-'}",
                flush=True,
            )
            resolution = await resolve_pbx_call(
                query_cdr=query_cdr,
                download_recording=download_recording,
                extension=extension,
                phone=payload.get("phone") or "",
                call_time=call_time,
                is_incoming=bool(payload.get("is_incoming")),
                was_answered=bool(payload.get("was_answered")),
                call_duration_sec=int(payload.get("duration_seconds") or 0),
                call_from_label=payload.get("call_from_label"),
                answer_time=answer_time,
                call_end_time=call_end_time,
                work_dir=RECORDINGS_DIR / job_id,
                verify_linkedid=verify_linkedid,
                exclude_linkedids=used_linkedids,
                claim_linkedid=_claim_linkedid,
                release_linkedid=_release_linkedid,
            )
            if resolution.cdr_info:
                cdr = resolution.cdr_info
                call_result_override, call_status_override, billsec = _apply_pbx_cdr_to_payload(
                    payload, cdr
                )
                if billsec > 0:
                    pbx_billsec = billsec
                kommo_jobs_db.update_job(job_id, payload_json=payload)
                if not resolution.recording_path:
                    if not _pbx_recording_still_expected(payload, cdr):
                        cdr_note_created = True
                print(
                    f"[kommo_call_worker] job {job_id}: CDR "
                    f"linkedid={cdr.linkedid} disposition={cdr.disposition} "
                    f"answered={cdr.was_answered} billsec={billsec} "
                    f"result={cdr.kommo_call_result}",
                    flush=True,
                )
            if resolution.recording_path:
                audio_path = resolution.recording_path
                upload_source = "miko_pbx"
                if resolution.cdr_duration and int(resolution.cdr_duration) > 0:
                    pbx_billsec = int(resolution.cdr_duration)
                if pbx_billsec and pbx_billsec > 0:
                    payload["duration_seconds"] = pbx_billsec
                kommo_jobs_db.update_job(
                    job_id,
                    recording_path=resolution.recording_path,
                    upload_source=upload_source,
                    payload_json=payload,
                )
            elif resolution.cdr_info is not None and not resolution.recording_path:
                cdr = resolution.cdr_info
                if not _pbx_recording_still_expected(payload, cdr):
                    print(
                        f"[kommo_call_worker] job {job_id}: CDR matched without recording "
                        f"(disposition={cdr.disposition}) — Kommo note only",
                        flush=True,
                    )

            recording_wait_reason = "Waiting for Miko CDR"
            if resolution.cdr_info and not resolution.recording_path:
                if _pbx_recording_still_expected(payload, resolution.cdr_info):
                    recording_wait_reason = "Waiting for Miko recording"

            if bool(payload.get("enable_recording_upload", True)) and bool(
                payload.get("was_answered")
            ):
                need_pbx_recording = not audio_path and (
                    resolution.cdr_info is None
                    or (
                        resolution.cdr_info is not None
                        and not resolution.recording_path
                        and _pbx_recording_still_expected(payload, resolution.cdr_info)
                    )
                )
                if need_pbx_recording:
                    retry = int(payload.get("pbx_recording_retry") or 0)
                    if retry < MAX_PBX_RECORDING_JOB_RETRIES:
                        payload["pbx_recording_retry"] = retry + 1
                        wait_idx = min(retry, len(JOB_RETRY_WAITS_SEC) - 1)
                        wait_sec = JOB_RETRY_WAITS_SEC[wait_idx]
                        payload["retry_after"] = (
                            datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
                        ).isoformat()
                        kommo_jobs_db.update_job(
                            job_id,
                            status="queued",
                            payload_json=payload,
                            reason=(
                                f"{recording_wait_reason} "
                                f"(retry {retry + 1}/{MAX_PBX_RECORDING_JOB_RETRIES}, next in {wait_sec}s)"
                            ),
                        )
                        msg = (
                            f"[kommo_call_worker] job {job_id}: {recording_wait_reason}, "
                            f"retry {retry + 1}/{MAX_PBX_RECORDING_JOB_RETRIES} in {wait_sec}s (non-blocking)"
                        )
                        log.info(msg)
                        print(msg, flush=True)
                        return
                    log.warning("job %s: CDR not matched after %s attempts", job_id, retry)
                    print(
                        f"[kommo_call_worker] job {job_id}: CDR/recording not ready after {retry} attempts "
                        f"— will create Kommo call note from client data",
                        flush=True,
                    )

        inbound_defer = int(payload.get("inbound_unanswered_defer") or 0)
        if (
            prefer_miko
            and bool(payload.get("is_incoming"))
            and not bool(payload.get("was_answered"))
            and not payload.get("pbx_linkedid")
            and not (audio_path and Path(audio_path).is_file())
            and inbound_defer < INBOUND_UNANSWERED_DEFER_MAX
        ):
            wait_sec = INBOUND_UNANSWERED_DEFER_WAITS_SEC[inbound_defer]
            payload["inbound_unanswered_defer"] = inbound_defer + 1
            payload["retry_after"] = (
                datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
            ).isoformat()
            kommo_jobs_db.update_job(
                job_id,
                status="queued",
                payload_json=payload,
                reason=(
                    f"Deferring missed inbound note ({inbound_defer + 1}/"
                    f"{INBOUND_UNANSWERED_DEFER_MAX}) — waiting for PBX CDR"
                ),
            )
            msg = (
                f"[kommo_call_worker] job {job_id}: defer missed inbound "
                f"{inbound_defer + 1}/{INBOUND_UNANSWERED_DEFER_MAX} in {wait_sec}s"
            )
            log.info(msg)
            print(msg, flush=True)
            return

        recording_wanted = bool(payload.get("enable_recording_upload", True)) and bool(
            payload.get("was_answered")
        )
        has_audio = bool(audio_path and Path(audio_path).is_file())
        retries_exhausted = int(payload.get("pbx_recording_retry") or 0) >= MAX_PBX_RECORDING_JOB_RETRIES

        if has_audio:
            file_hash = _sha256_file(audio_path or "")
            if file_hash:
                payload["recording_sha256"] = file_hash
                lead_hint = payload.get("lead_id")
                if not kommo_jobs_db.try_claim_recording_hash(
                    extension,
                    file_hash,
                    job_id,
                    phone=(payload.get("phone") or "").strip(),
                    lead_id=int(lead_hint) if lead_hint else None,
                ):
                    print(
                        f"[kommo_call_worker] job {job_id}: duplicate audio hash "
                        f"{file_hash[:12]}… — skipping recording upload",
                        flush=True,
                    )
                    kommo_jobs_db.update_job(
                        job_id,
                        status="skipped",
                        reason="Duplicate recording (same audio already uploaded)",
                        payload_json=payload,
                    )
                    return

        if recording_wanted and not has_audio and not retries_exhausted and not cdr_note_created:
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                reason="PBX recording not found or not ready",
            )
            print(f"[kommo_call_worker] job {job_id} failed: no PBX recording", flush=True)
            return

        if recording_wanted and not has_audio and (retries_exhausted or cdr_note_created):
            audio_path = None
            upload_source = None

        if not bool(payload.get("enable_recording_upload", True)):
            audio_path = None

        if not payload.get("lead_id"):
            phone = (payload.get("phone") or "").strip()
            if phone:
                try:
                    lead_id, _contact_id = await client.resolve_upload_target(phone)
                    if lead_id:
                        payload["lead_id"] = lead_id
                        print(
                            f"[kommo_call_worker] job {job_id}: resolved lead_id="
                            f"{lead_id} for {phone}",
                            flush=True,
                        )
                except Exception as exc:
                    log.warning("job %s lead lookup failed: %s", job_id, exc)

        client_duration = int(payload.get("duration_seconds") or 0)
        duration_seconds = pbx_billsec if pbx_billsec and pbx_billsec > 0 else client_duration
        if duration_seconds != client_duration and client_duration > 0:
            print(
                f"[kommo_call_worker] job {job_id}: duration {client_duration}s "
                f"→ {duration_seconds}s (PBX CDR)",
                flush=True,
            )

        outcome: ProcessCallOutcome = await client.process_call(
            payload.get("phone") or "",
            is_incoming=bool(payload.get("is_incoming")),
            duration_seconds=duration_seconds,
            was_answered=bool(payload.get("was_answered")),
            audio_path=audio_path,
            call_time=_parse_call_time(payload.get("call_time") or ""),
            lead_id=payload.get("lead_id"),
            call_from_label=payload.get("call_from_label"),
            did=(payload.get("did") or "").strip() or None,
            upload_source=upload_source,
            call_result=call_result_override,
            call_status=call_status_override,
        )

        final_source = upload_source or outcome.upload_source
        note_without_recording = (recording_wanted and not has_audio and cdr_note_created) or (
            recording_wanted and not has_audio and retries_exhausted
        )
        if outcome.success and outcome.upload_status == "uploaded":
            reason = outcome.reason
            if note_without_recording and cdr_note_created:
                reason = f"Call note from PBX CDR ({call_result_override or 'no recording'})"
            elif note_without_recording:
                reason = "Call note created; PBX recording unavailable"
            kommo_jobs_db.update_job(
                job_id,
                status="uploaded",
                lead_id=outcome.lead_id,
                upload_source=final_source,
                reason=reason,
                payload_json=payload,
            )
            file_hash = payload.get("recording_sha256")
            if not file_hash and has_audio and audio_path:
                file_hash = _sha256_file(audio_path)
            if file_hash and outcome.lead_id:
                kommo_jobs_db.register_recording_hash(
                    extension,
                    file_hash,
                    job_id,
                    phone=(payload.get("phone") or "").strip(),
                    lead_id=outcome.lead_id,
                )
            print(
                f"[kommo_call_worker] job {job_id} uploaded lead_id={outcome.lead_id} "
                f"source={final_source} linkedid={payload.get('pbx_linkedid') or '-'}",
                flush=True,
            )
        elif outcome.upload_status == "not_uploaded":
            keep_recording_files = True
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                lead_id=outcome.lead_id,
                upload_source=final_source,
                recording_path=audio_path if has_audio else job.get("recording_path"),
                reason=outcome.reason or "Recording upload failed",
            )
            print(
                f"[kommo_call_worker] job {job_id} failed: {outcome.reason or 'Recording upload failed'}",
                flush=True,
            )
        else:
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                lead_id=outcome.lead_id,
                reason=outcome.reason or outcome.upload_status,
            )
            print(
                f"[kommo_call_worker] job {job_id} failed: {outcome.reason or outcome.upload_status}",
                flush=True,
            )
    except Exception as exc:
        log.exception("job %s failed: %s", job_id, exc)
        print(f"[kommo_call_worker] job {job_id} exception: {exc}", flush=True)
        kommo_jobs_db.update_job(job_id, status="failed", reason=str(exc))
    finally:
        await client.close()
        if not keep_recording_files:
            _cleanup_job_files(job_id)


def _cleanup_job_files(job_id: str) -> None:
    folder = RECORDINGS_DIR / job_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
