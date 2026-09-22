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
from kommo_recording import (
    resolve_pbx_call,
    JOB_RETRY_WAITS_SEC,
    pbx_recording_definitely_absent,
)

log = logging.getLogger("kommo_call_worker")

WORKER_COUNT = 10
MAX_RECORDING_RETRIES = 6
QUEUE_HIGH_WATERMARK = 15
MAX_RECORDING_RETRIES_CAP = 12
JOBS_RETENTION_DAYS = 7
RETENTION_CLEANUP_INTERVAL_SEC = 3600
CLIENT_RECORDING_WAIT_SECONDS = 360
RECORDINGS_DIR = Path(__file__).resolve().parent / "kommo_upload_recordings"

_running = False
_tasks: list[asyncio.Task] = []


def configure(cfg: dict[str, Any] | None = None) -> None:
    """Apply Kommo worker settings from config.yaml (safe to call multiple times)."""
    global WORKER_COUNT, MAX_RECORDING_RETRIES, QUEUE_HIGH_WATERMARK, JOBS_RETENTION_DAYS
    if not cfg:
        return
    WORKER_COUNT = max(1, min(32, int(cfg.get("kommo_worker_count") or WORKER_COUNT)))
    MAX_RECORDING_RETRIES = max(
        2, min(20, int(cfg.get("kommo_max_recording_retries") or MAX_RECORDING_RETRIES))
    )
    QUEUE_HIGH_WATERMARK = max(
        5, int(cfg.get("kommo_queue_high_watermark") or QUEUE_HIGH_WATERMARK)
    )
    JOBS_RETENTION_DAYS = max(
        1, min(90, int(cfg.get("kommo_jobs_retention_days") or JOBS_RETENTION_DAYS))
    )


def _effective_max_recording_retries() -> int:
    """Add extra retries when many jobs are waiting — typical during call peaks."""
    pending = kommo_jobs_db.count_pending_recording_jobs(include_processing=True)
    extra = max(0, (pending - QUEUE_HIGH_WATERMARK) // 5) * 2
    return min(MAX_RECORDING_RETRIES + extra, MAX_RECORDING_RETRIES_CAP)


def _retry_wait_seconds(retry_index: int) -> int:
    waits = JOB_RETRY_WAITS_SEC
    idx = min(max(0, retry_index), len(waits) - 1)
    return waits[idx]


def _schedule_recording_retry(
    job_id: str,
    payload: dict[str, Any],
    *,
    reason: str,
    log_msg: str,
) -> bool:
    """Requeue job for another Miko CDR/recording attempt. Returns False if cap reached."""
    retry = int(payload.get("pbx_recording_retry") or 0)
    max_retries = _effective_max_recording_retries()
    if retry >= max_retries:
        return False
    payload["pbx_recording_retry"] = retry + 1
    wait_sec = _retry_wait_seconds(retry)
    payload["retry_after"] = (
        datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
    ).isoformat()
    kommo_jobs_db.update_job(
        job_id,
        status="queued",
        payload_json=payload,
        reason=f"{reason} (retry {retry + 1}/{max_retries}, next in {wait_sec}s)",
    )
    log.info(log_msg)
    print(log_msg, flush=True)
    return True


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


def _effective_was_answered(
    payload: dict[str, Any], *, pbx_billsec: Optional[int] = None
) -> bool:
    """Answered = client flag, answer_time, or PBX CDR billsec (not client ring timer)."""
    if bool(payload.get("was_answered")):
        return True
    if payload.get("answer_time"):
        return True
    if pbx_billsec is not None and int(pbx_billsec) >= 3:
        return True
    return False


def _should_wait_for_pbx_cdr(
    payload: dict[str, Any], *, pbx_billsec: Optional[int] = None
) -> bool:
    """Wait for Miko CDR before uploading when recording may exist (CDR is source of truth)."""
    if not bool(payload.get("enable_recording_upload", True)):
        return False
    if _effective_was_answered(payload, pbx_billsec=pbx_billsec):
        return True
    dur = int(payload.get("duration_seconds") or 0)
    # Outbound client timer includes ring time — need CDR to know real talk / recording.
    return dur >= 8 and not bool(payload.get("is_incoming"))


def _recording_upload_wanted(
    payload: dict[str, Any], *, pbx_billsec: Optional[int] = None
) -> bool:
    if not bool(payload.get("enable_recording_upload", True)):
        return False
    return _effective_was_answered(payload, pbx_billsec=pbx_billsec)


def _apply_cdr_truth_to_payload(
    payload: dict[str, Any],
    cdr: Any,
) -> None:
    """Overwrite client timing with PBX CDR (client duration often includes ring time)."""
    client_dur = int(payload.get("duration_seconds") or 0)
    if client_dur > 0 and "client_duration_seconds" not in payload:
        payload["client_duration_seconds"] = client_dur
    cdr_billsec = int(getattr(cdr, "billsec", 0) or 0)
    cdr_answered = bool(getattr(cdr, "was_answered", False))
    payload["pbx_billsec"] = cdr_billsec
    payload["was_answered"] = cdr_answered or _effective_was_answered(
        payload, pbx_billsec=cdr_billsec
    )
    if cdr_billsec > 0:
        payload["duration_seconds"] = cdr_billsec
    elif not cdr_answered:
        payload["duration_seconds"] = 0
    if client_dur >= 10 and cdr_billsec <= 1 and not cdr_answered:
        print(
            f"[kommo_call_worker] client duration {client_dur}s but CDR "
            f"billsec={cdr_billsec} disposition={getattr(cdr, 'disposition', '')} "
            f"— using PBX truth",
            flush=True,
        )


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
    run_retention_cleanup()
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
    _tasks.append(
        asyncio.create_task(_retention_cleanup_loop(), name="kommo-retention-cleanup")
    )
    msg = (
        f"[kommo_call_worker] Started {WORKER_COUNT} Kommo call upload workers "
        f"(max_recording_retries={MAX_RECORDING_RETRIES}, "
        f"queue_high_watermark={QUEUE_HIGH_WATERMARK}, "
        f"job_retention_days={JOBS_RETENTION_DAYS})"
    )
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


def run_retention_cleanup() -> int:
    """Delete Kommo call jobs older than configured retention (default 7 days)."""
    removed = kommo_jobs_db.cleanup_old_jobs(days=JOBS_RETENTION_DAYS)
    if removed:
        msg = (
            f"[kommo_call_worker] retention cleanup: removed {removed} job(s) "
            f"older than {JOBS_RETENTION_DAYS} day(s)"
        )
        log.info(msg)
        print(msg, flush=True)
    return removed


async def _retention_cleanup_loop() -> None:
    while _running:
        try:
            run_retention_cleanup()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("Kommo retention cleanup error: %s", exc)
            print(f"[kommo_call_worker] retention cleanup error: {exc}", flush=True)
        try:
            await asyncio.sleep(RETENTION_CLEANUP_INTERVAL_SEC)
        except asyncio.CancelledError:
            break


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

    try:
        audio_path: Optional[str] = job.get("recording_path")
        upload_source: Optional[str] = None

        client_recording = bool(payload.get("client_recording_enabled"))
        connection_slot = (payload.get("connection_slot") or "main").lower()
        prefer_miko = connection_slot != "secondary" and not client_recording

        if prefer_miko and not audio_path and payload.get("admin_reupload"):
            sibling = kommo_jobs_db.find_miko_recording_sibling_job(
                extension,
                (payload.get("phone") or "").strip(),
                exclude_job_id=job_id,
            )
            if sibling:
                sib_payload = sibling.get("payload") or {}
                sib_lid = (sib_payload.get("pbx_linkedid") or "").strip()
                if not sib_lid:
                    rec_name = Path(str(sibling.get("recording_path") or "")).stem
                    if rec_name.startswith("mikopbx_"):
                        sib_lid = rec_name[len("mikopbx_") :]
                sib_path = (sibling.get("recording_path") or "").strip()
                if sib_lid and not (payload.get("pbx_linkedid") or "").strip():
                    payload["pbx_linkedid"] = sib_lid
                    kommo_jobs_db.update_job(job_id, payload_json=payload)
                if sib_path and Path(sib_path).is_file():
                    audio_path = sib_path
                    upload_source = sibling.get("upload_source") or "miko_pbx"
                    print(
                        f"[kommo_call_worker] job {job_id}: admin reupload reusing Miko file "
                        f"from sibling job {sibling['id']} linkedid={sib_lid or '-'}",
                        flush=True,
                    )

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
        cdr_may_have_recording = False
        recording_definitely_absent = False
        pbx_billsec: Optional[int] = None

        if audio_path and Path(audio_path).is_file():
            upload_source = "client"
        elif prefer_miko:
            call_time = _parse_call_time(payload.get("call_time") or "")
            answer_time_raw = payload.get("answer_time")
            answer_time = _parse_call_time(answer_time_raw) if answer_time_raw else None
            call_end_raw = payload.get("call_end_time")
            call_end_time = _parse_call_time(call_end_raw) if call_end_raw else None
            client_duration = int(payload.get("duration_seconds") or 0)
            job_created_at = _parse_call_time(job.get("created_at") or "")
            if job_created_at and call_time > job_created_at + timedelta(seconds=20):
                if call_end_time is None:
                    call_end_time = job_created_at
                if answer_time is None and client_duration > 0:
                    answer_time = job_created_at - timedelta(seconds=client_duration)
                print(
                    f"[kommo_call_worker] job {job_id}: client call_time is "
                    f"{(call_time - job_created_at).total_seconds():.0f}s after job enqueue "
                    f"— using job time anchors for CDR match",
                    flush=True,
                )
            elif call_end_time is None and client_duration > 0:
                call_end_time = call_time + timedelta(seconds=client_duration)
            used_linkedids = kommo_jobs_db.list_used_pbx_linkedids(
                extension,
                exclude_job_id=job_id,
                phone=(payload.get("phone") or "").strip(),
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
                job_created_at=job_created_at,
                work_dir=RECORDINGS_DIR / job_id,
                verify_linkedid=verify_linkedid,
                exclude_linkedids=used_linkedids,
                claim_linkedid=_claim_linkedid,
                release_linkedid=_release_linkedid,
                known_linkedid=(payload.get("pbx_linkedid") or "").strip() or None,
                quick_cdr_pass=int(payload.get("pbx_recording_retry") or 0) > 0,
            )
            if resolution.cdr_info:
                payload["pbx_linkedid"] = resolution.cdr_info.linkedid
                if resolution.cdr_info.billsec > 0:
                    pbx_billsec = resolution.cdr_info.billsec
                elif resolution.cdr_info.duration > 0:
                    pbx_billsec = resolution.cdr_info.duration
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
            elif resolution.cdr_info:
                cdr = resolution.cdr_info
                if cdr.billsec > 0:
                    pbx_billsec = cdr.billsec
                recording_wanted_now = _recording_upload_wanted(
                    payload, pbx_billsec=pbx_billsec
                )
                cdr_may_have_recording = cdr.was_answered and (
                    cdr.has_recording or cdr.billsec > 0
                )
                recording_definitely_absent = pbx_recording_definitely_absent(cdr)
                if recording_definitely_absent:
                    if cdr_may_have_recording:
                        print(
                            f"[kommo_call_worker] job {job_id}: CDR work_completed=1 "
                            f"without recordingfile (linkedid={cdr.linkedid}) — skipping retries",
                            flush=True,
                        )
                    cdr_may_have_recording = False
                if recording_wanted_now and cdr_may_have_recording and not resolution.recording_path:
                    if _schedule_recording_retry(
                        job_id,
                        payload,
                        reason="Waiting for Miko recording",
                        log_msg=(
                            f"[kommo_call_worker] job {job_id}: CDR matched but recording not ready "
                            f"(queue={kommo_jobs_db.count_pending_recording_jobs()})"
                        ),
                    ):
                        return
                _apply_cdr_truth_to_payload(payload, cdr)
                payload["pbx_linkedid"] = cdr.linkedid
                call_result_override = cdr.kommo_call_result
                call_status_override = cdr.kommo_call_status
                if cdr.caller_id and not payload.get("call_from_label"):
                    payload["call_from_label"] = cdr.caller_id
                # Note-only metadata from CDR — upload only when recording is not expected.
                if not recording_wanted_now or not cdr_may_have_recording or recording_definitely_absent:
                    cdr_note_created = True
                kommo_jobs_db.update_job(job_id, payload_json=payload)
                print(
                    f"[kommo_call_worker] job {job_id}: CDR note "
                    f"linkedid={cdr.linkedid} disposition={cdr.disposition} "
                    f"result={cdr.kommo_call_result} note_only={cdr_note_created}",
                    flush=True,
                )
            elif _should_wait_for_pbx_cdr(payload, pbx_billsec=pbx_billsec):
                if _schedule_recording_retry(
                    job_id,
                    payload,
                    reason="Waiting for Miko CDR",
                    log_msg=(
                        f"[kommo_call_worker] job {job_id}: CDR not ready "
                        f"(queue={kommo_jobs_db.count_pending_recording_jobs()})"
                    ),
                ):
                    return
                retry = int(payload.get("pbx_recording_retry") or 0)
                log.warning("job %s: CDR not matched after %s attempts", job_id, retry)
                print(
                    f"[kommo_call_worker] job {job_id}: CDR not matched after {retry} attempts "
                    f"— will fail instead of empty Kommo card",
                    flush=True,
                )

        recording_wanted = _recording_upload_wanted(payload, pbx_billsec=pbx_billsec)
        has_audio = bool(audio_path and Path(audio_path).is_file())
        max_retries = _effective_max_recording_retries()
        retries_exhausted = int(payload.get("pbx_recording_retry") or 0) >= max_retries

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
                    owner = kommo_jobs_db.get_recording_hash_owner(extension, file_hash)
                    owner_session = (owner or {}).get("session_id") if owner else None
                    this_session = payload.get("session_id")
                    if owner_session and this_session and owner_session == this_session:
                        print(
                            f"[kommo_call_worker] job {job_id}: duplicate audio hash "
                            f"{file_hash[:12]}… — skipping duplicate submit",
                            flush=True,
                        )
                        kommo_jobs_db.update_job(
                            job_id,
                            status="skipped",
                            reason="Duplicate recording (same audio already uploaded)",
                            payload_json=payload,
                        )
                        return
                    print(
                        f"[kommo_call_worker] job {job_id}: audio hash {file_hash[:12]}… "
                        f"matches another call (owner session={owner_session or '-'}, "
                        f"this session={this_session or '-'}) — note only, no recording",
                        flush=True,
                    )
                    audio_path = None
                    has_audio = False
                    upload_source = None

        if recording_wanted and not has_audio and cdr_may_have_recording and not recording_definitely_absent:
            if not retries_exhausted and _schedule_recording_retry(
                job_id,
                payload,
                reason="Waiting for PBX recording before Kommo upload",
                log_msg=(
                    f"[kommo_call_worker] job {job_id}: deferring Kommo upload until recording "
                    f"is ready (queue={kommo_jobs_db.count_pending_recording_jobs()})"
                ),
            ):
                return
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                reason=(
                    "PBX recording not ready after retries "
                    f"({int(payload.get('pbx_recording_retry') or 0)}/{max_retries}); "
                    "no empty Kommo card created"
                ),
                payload_json=payload,
            )
            print(
                f"[kommo_call_worker] job {job_id} failed: recording still missing after "
                f"{max_retries} retries — empty card blocked",
                flush=True,
            )
            return

        if recording_wanted and not has_audio and not cdr_note_created and not retries_exhausted:
            if _schedule_recording_retry(
                job_id,
                payload,
                reason="Waiting for Miko CDR/recording",
                log_msg=(
                    f"[kommo_call_worker] job {job_id}: deferring until PBX CDR/recording "
                    f"(queue={kommo_jobs_db.count_pending_recording_jobs()})"
                ),
            ):
                return
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                reason="PBX recording not found or not ready",
                payload_json=payload,
            )
            print(f"[kommo_call_worker] job {job_id} failed: no PBX recording", flush=True)
            return

        if recording_wanted and not has_audio and cdr_note_created:
            talk_sec = (
                int(pbx_billsec)
                if pbx_billsec and int(pbx_billsec) > 0
                else int(payload.get("duration_seconds") or 0)
            )
            if (
                talk_sec >= 5
                and cdr_may_have_recording
                and not recording_definitely_absent
            ):
                if not retries_exhausted and _schedule_recording_retry(
                    job_id,
                    payload,
                    reason="Waiting for Miko recording",
                    log_msg=(
                        f"[kommo_call_worker] job {job_id}: answered call "
                        f"({talk_sec}s) — deferring note until recording is ready"
                    ),
                ):
                    return
            # IVR reject / cancel / no talk — intentional note-only card.
            audio_path = None
            upload_source = None
        elif recording_wanted and not has_audio and retries_exhausted:
            sibling = kommo_jobs_db.find_miko_recording_sibling_job(
                extension,
                (payload.get("phone") or "").strip(),
                exclude_job_id=job_id,
            )
            if sibling and (
                sibling.get("upload_source") == "miko_pbx"
                or (sibling.get("recording_path") or "").strip()
            ):
                kommo_jobs_db.update_job(
                    job_id,
                    status="skipped",
                    reason=(
                        f"Duplicate submit — recording already uploaded by job {sibling['id']}"
                    ),
                    payload_json=payload,
                )
                print(
                    f"[kommo_call_worker] job {job_id}: skipped duplicate; "
                    f"recording already on job {sibling['id']}",
                    flush=True,
                )
                return
            kommo_jobs_db.release_pbx_linkedid_claims_for_job(job_id)
            client_answered = bool(payload.get("was_answered")) or bool(
                payload.get("answer_time")
            )
            client_dur = int(payload.get("duration_seconds") or 0)
            # Answered / long outbound: CDR match can fail (clock skew, trunk leg) but
            # client metadata is still worth a Kommo note — better than a hard fail.
            if client_answered or (client_dur >= 8 and not bool(payload.get("is_incoming"))):
                print(
                    f"[kommo_call_worker] job {job_id}: no PBX CDR/recording after "
                    f"{max_retries} retries — client fallback note "
                    f"(answered={client_answered}, duration={client_dur}s)",
                    flush=True,
                )
            else:
                kommo_jobs_db.update_job(
                    job_id,
                    status="failed",
                    reason=(
                        "PBX CDR/recording unavailable after retries "
                        f"({int(payload.get('pbx_recording_retry') or 0)}/{max_retries}); "
                        "no empty Kommo card created"
                    ),
                    payload_json=payload,
                )
                print(
                    f"[kommo_call_worker] job {job_id} failed: no CDR/recording after retries "
                    f"— empty card blocked",
                    flush=True,
                )
                return

        if not bool(payload.get("enable_recording_upload", True)):
            audio_path = None

        # Lead selection is done inside process_call (contact → newest open lead → fallback).
        # Do not pre-resolve lead_id here — an early phone-only search picks stale leads.

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
            upload_source=upload_source,
            call_result=call_result_override,
            call_status=call_status_override,
            did=payload.get("pbx_did") or payload.get("did"),
            linkedid=payload.get("pbx_linkedid"),
        )

        final_source = upload_source or outcome.upload_source
        note_without_recording = recording_wanted and not has_audio
        if outcome.success and outcome.upload_status == "uploaded":
            reason = outcome.reason
            if note_without_recording and cdr_note_created:
                reason = f"Call note from PBX CDR ({call_result_override or 'no recording'})"
            elif note_without_recording:
                reason = "Call note from client; PBX CDR/recording unavailable"
            kommo_jobs_db.update_job(
                job_id,
                status="uploaded",
                lead_id=outcome.lead_id,
                contact_id=outcome.contact_id,
                crm_entity=(
                    "lead"
                    if outcome.lead_id
                    else ("contact" if outcome.contact_id else None)
                ),
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
                f"contact_id={outcome.contact_id} "
                f"source={final_source} linkedid={payload.get('pbx_linkedid') or '-'}",
                flush=True,
            )
        elif outcome.upload_status == "not_uploaded":
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                lead_id=outcome.lead_id,
                upload_source=final_source,
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
        _cleanup_job_files(job_id)


def _cleanup_job_files(job_id: str) -> None:
    folder = RECORDINGS_DIR / job_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
