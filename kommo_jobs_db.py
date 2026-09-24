"""SQLite storage for Kommo process-call jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from phone_normalize import phones_match_for_dedup


def _parse_call_time_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

_DB_PATH: Optional[Path] = None


def init_db(db_path: str | Path | None = None) -> None:
    global _DB_PATH
    if db_path is not None:
        _DB_PATH = Path(db_path)
    elif _DB_PATH is None:
        _DB_PATH = Path(__file__).resolve().parent / "kommo_jobs.sqlite"
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kommo_call_jobs (
                id TEXT PRIMARY KEY,
                extension TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                lead_id INTEGER,
                upload_source TEXT,
                reason TEXT,
                recording_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Older DBs miss contact_id/crm_entity — the web CRM button needs them
        # when a call is attached to a contact instead of a lead.
        for ddl in (
            "ALTER TABLE kommo_call_jobs ADD COLUMN contact_id INTEGER",
            "ALTER TABLE kommo_call_jobs ADD COLUMN crm_entity TEXT",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kommo_call_jobs_status ON kommo_call_jobs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kommo_call_jobs_extension ON kommo_call_jobs(extension)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kommo_pbx_linkedid_claims (
                linkedid TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                extension TEXT NOT NULL,
                phone TEXT,
                session_id TEXT,
                call_time TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kommo_linkedid_claims_ext
            ON kommo_pbx_linkedid_claims(extension, created_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kommo_recording_hashes (
                sha256 TEXT NOT NULL,
                extension TEXT NOT NULL,
                phone TEXT,
                lead_id INTEGER,
                job_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (sha256, extension)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kommo_cdr_entity_processed (
                linkedid TEXT PRIMARY KEY,
                phone TEXT NOT NULL DEFAULT '',
                extension TEXT NOT NULL DEFAULT '',
                call_start TEXT,
                source TEXT NOT NULL DEFAULT 'cdr_worker',
                rule_id INTEGER,
                task_created INTEGER NOT NULL DEFAULT 0,
                task_id INTEGER,
                contact_id INTEGER,
                lead_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute("ALTER TABLE kommo_cdr_entity_processed ADD COLUMN task_id INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        init_db()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(
    extension: str,
    dedup_key: str,
    payload: dict[str, Any],
    *,
    status: str = "queued",
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = _now_iso()
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO kommo_call_jobs
                    (id, extension, dedup_key, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, extension, dedup_key, json.dumps(payload), status, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM kommo_call_jobs WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if row:
                return _row_to_dict(row)
            raise
    return get_job(job_id) or {}


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_job_by_dedup(dedup_key: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_job(job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    allowed = {
        "status",
        "lead_id",
        "contact_id",
        "crm_entity",
        "upload_source",
        "reason",
        "recording_path",
        "payload_json",
    }
    parts: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        parts.append(f"{key} = ?")
        if key == "payload_json" and isinstance(value, dict):
            values.append(json.dumps(value))
        else:
            values.append(value)
    if not parts:
        return get_job(job_id)
    parts.append("updated_at = ?")
    values.append(_now_iso())
    values.append(job_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE kommo_call_jobs SET {', '.join(parts)} WHERE id = ?",
            values,
        )
        conn.commit()
    return get_job(job_id)


def count_pending_recording_jobs(*, include_processing: bool = False) -> int:
    """Jobs waiting for a worker or for Miko CDR/recording (queue depth indicator)."""
    statuses = ["queued", "waiting_recording"]
    if include_processing:
        statuses.append("processing")
    placeholders = ",".join("?" for _ in statuses)
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt FROM kommo_call_jobs
            WHERE status IN ({placeholders})
            AND (
                status = 'processing'
                OR json_extract(payload_json, '$.retry_after') IS NULL
                OR json_extract(payload_json, '$.retry_after') <= ?
            )
            """,
            (*statuses, now),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def list_jobs_by_status(statuses: list[str], limit: int = 50) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM kommo_call_jobs
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (*statuses, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_next_job(statuses: list[str]) -> Optional[dict[str, Any]]:
    """Atomically pick one ready job (oldest first) and mark it processing."""
    placeholders = ",".join("?" for _ in statuses)
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT id FROM kommo_call_jobs
            WHERE status IN ({placeholders})
            AND (
                json_extract(payload_json, '$.retry_after') IS NULL
                OR json_extract(payload_json, '$.retry_after') <= ?
            )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (*statuses, now),
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            f"""
            UPDATE kommo_call_jobs
            SET status = 'processing', updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (now, row["id"], *statuses),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM kommo_call_jobs WHERE id = ?", (row["id"],)
        ).fetchone()
    return _row_to_dict(claimed) if claimed else None


def recover_orphaned_processing_jobs() -> int:
    """Requeue jobs left in processing after a service restart killed workers."""
    now = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE kommo_call_jobs
            SET status = 'queued',
                reason = 'Requeued after service restart',
                updated_at = ?
            WHERE status = 'processing'
            """,
            (now,),
        )
        conn.commit()
        return cur.rowcount


def list_uploaded_jobs_for_extension(
    extension: str,
    *,
    hours: int = 72,
    limit: int = 100,
) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM kommo_call_jobs
            WHERE extension = ? AND status = 'uploaded' AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (extension, cutoff, limit),
        ).fetchall()
    items = []
    for row in rows:
        job = _row_to_dict(row)
        payload = job.get("payload") or {}
        items.append(
            {
                "id": job["id"],
                "phone": payload.get("phone"),
                "call_time": payload.get("call_time"),
                "lead_id": job.get("lead_id"),
                "contact_id": job.get("contact_id"),
                "crm_entity": job.get("crm_entity"),
                "status": job["status"],
                "upload_source": job.get("upload_source"),
                "reason": job.get("reason"),
                "created_at": job.get("created_at"),
            }
        )
    return items


def _is_duplicate_call_submit(
    *,
    call_dt: datetime,
    other_dt: datetime,
    session_id: str,
    other_session: str,
) -> bool:
    """True when two process-call payloads refer to the same call instance.

    Consecutive calls to the same number must NOT match — only double-submits
    (web + desktop, or SendCallDetails retries) should reuse an existing job.
    """
    session_id = (session_id or "").strip()
    other_session = (other_session or "").strip()
    time_diff = abs((other_dt - call_dt).total_seconds())

    if session_id and other_session:
        # Distinct WebRTC/SIP sessions are separate calls even to the same number.
        if session_id != other_session:
            return False
        return time_diff <= 120

    if session_id or other_session:
        # One side missing session id — only merge very tight double-submits.
        return time_diff <= 5

    # Legacy clients without session_id: short window only.
    return time_diff <= 10


def _is_same_call_resubmit(
    *,
    call_dt: datetime,
    other_dt: datetime,
    session_id: str,
    other_session: str,
    other_job_created_at: Optional[str] = None,
) -> bool:
    """Broader match for reusing a failed/skipped job after Kommo/CDR errors."""
    if _is_duplicate_call_submit(
        call_dt=call_dt,
        other_dt=other_dt,
        session_id=session_id,
        other_session=other_session,
    ):
        return True
    time_diff = abs((other_dt - call_dt).total_seconds())
    session_id = (session_id or "").strip()
    other_session = (other_session or "").strip()
    if session_id and other_session and session_id != other_session:
        return False
    if time_diff <= 180:
        return True
    other_created = _parse_call_time_iso(other_job_created_at or "")
    if other_created is not None:
        # Softphone often enqueues right after hangup while call_time in payload is skewed.
        if abs((call_dt - other_created).total_seconds()) <= 900:
            return True
        if abs((other_dt - other_created).total_seconds()) <= 900:
            return True
    return False


def find_overlapping_active_job(
    extension: str,
    phone: str,
    call_time: str,
    *,
    session_id: Optional[str] = None,
    window_sec: int = 60,
    except_job_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Find an in-flight, uploaded, or recently failed job for the same call."""
    phone = (phone or "").strip()
    call_dt = _parse_call_time_iso(call_time)
    if not phone or call_dt is None:
        return None
    cutoff = (call_dt - timedelta(seconds=max(30, window_sec * 2))).isoformat()
    statuses = (
        "queued",
        "waiting_recording",
        "processing",
        "uploaded",
        "failed",
        "skipped",
    )
    placeholders = ",".join("?" * len(statuses))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM kommo_call_jobs
            WHERE extension = ?
              AND created_at >= ?
              AND status IN ({placeholders})
            ORDER BY created_at ASC
            """,
            (extension, cutoff, *statuses),
        ).fetchall()
    best_failed: Optional[dict[str, Any]] = None
    for row in rows:
        if except_job_id and row["id"] == except_job_id:
            continue
        job = _row_to_dict(row)
        payload = job.get("payload") or {}
        if (payload.get("phone") or "").strip() != phone:
            continue
        other_dt = _parse_call_time_iso(str(payload.get("call_time") or ""))
        if other_dt is None:
            continue
        other_session = str(payload.get("session_id") or "")
        status = str(job.get("status") or "")
        if status in ("failed", "skipped"):
            if _is_same_call_resubmit(
                call_dt=call_dt,
                other_dt=other_dt,
                session_id=str(session_id or ""),
                other_session=other_session,
                other_job_created_at=job.get("created_at"),
            ):
                best_failed = job
            continue
        if not _is_duplicate_call_submit(
            call_dt=call_dt,
            other_dt=other_dt,
            session_id=str(session_id or ""),
            other_session=other_session,
        ):
            continue
        if abs((other_dt - call_dt).total_seconds()) <= window_sec:
            return job
    return best_failed


def requeue_job_for_overlap_resubmit(
    job_id: str,
    payload_patch: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Revive a failed/skipped duplicate job instead of creating a new one."""
    job = get_job(job_id)
    if not job:
        return None
    status = str(job.get("status") or "")
    if status not in ("failed", "skipped"):
        return job
    payload = dict(job.get("payload") or {})
    patch = dict(payload_patch or {})
    for key in (
        "phone",
        "call_time",
        "session_id",
        "lead_id",
        "answer_time",
        "call_end_time",
        "duration_seconds",
        "was_answered",
        "call_from_label",
        "is_incoming",
        "enable_recording_upload",
        "client_recording_enabled",
        "connection_slot",
    ):
        val = patch.get(key)
        if val is None or val == "":
            continue
        if key == "lead_id":
            try:
                if int(val) > 0:
                    payload["lead_id"] = int(val)
            except (TypeError, ValueError):
                pass
            continue
        if key == "session_id" and payload.get("session_id"):
            continue
        if key == "call_time":
            new_ct = _parse_call_time_iso(str(val))
            old_ct = _parse_call_time_iso(str(payload.get("call_time") or ""))
            if new_ct and old_ct and new_ct >= old_ct:
                continue
        payload[key] = val
    payload.pop("retry_after", None)
    payload.pop("recording_sha256", None)
    payload["pbx_recording_retry"] = 0
    payload.pop("admin_reupload", None)
    release_pbx_linkedid_claims_for_job(job_id)
    clear_recording_hash_for_job(job_id)
    return update_job(
        job_id,
        status="queued",
        reason=None,
        upload_source=None,
        recording_path=None,
        lead_id=payload.get("lead_id") or job.get("lead_id"),
        payload_json=payload,
    )


def is_recording_hash_used(
    extension: str,
    sha256: str,
    *,
    hours: int = 72,
    exclude_job_id: Optional[str] = None,
) -> bool:
    digest = (sha256 or "").strip().lower()
    if not digest:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT job_id FROM kommo_recording_hashes
            WHERE extension = ? AND sha256 = ? AND created_at >= ?
            """,
            (extension, digest, cutoff),
        ).fetchone()
    if not row:
        return False
    if exclude_job_id and row["job_id"] == exclude_job_id:
        return False
    return True


def register_recording_hash(
    extension: str,
    sha256: str,
    job_id: str,
    *,
    phone: Optional[str] = None,
    lead_id: Optional[int] = None,
) -> None:
    digest = (sha256 or "").strip().lower()
    if not digest:
        return
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO kommo_recording_hashes
                (sha256, extension, phone, lead_id, job_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (digest, extension, phone, lead_id, job_id, now),
        )
        conn.commit()


def try_claim_recording_hash(
    extension: str,
    sha256: str,
    job_id: str,
    *,
    phone: Optional[str] = None,
    lead_id: Optional[int] = None,
) -> bool:
    """Reserve audio hash before Kommo upload (prevents parallel duplicate uploads)."""
    digest = (sha256 or "").strip().lower()
    if not digest:
        return False
    now = _now_iso()
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO kommo_recording_hashes
                    (sha256, extension, phone, lead_id, job_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (digest, extension, phone, lead_id, job_id, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            row = conn.execute(
                """
                SELECT job_id FROM kommo_recording_hashes
                WHERE sha256 = ? AND extension = ?
                """,
                (digest, extension),
            ).fetchone()
            return bool(row and row["job_id"] == job_id)


def get_recording_hash_owner(extension: str, sha256: str) -> Optional[dict[str, Any]]:
    """Return the job that already uploaded this audio hash (for cross-call dedup)."""
    digest = (sha256 or "").strip().lower()
    if not digest:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT job_id FROM kommo_recording_hashes
            WHERE sha256 = ? AND extension = ?
            """,
            (digest, extension),
        ).fetchone()
        if not row:
            return None
        job = get_job(row["job_id"])
    if not job:
        return {"job_id": row["job_id"]}
    payload = job.get("payload") or {}
    return {
        "job_id": job["id"],
        "session_id": payload.get("session_id"),
        "phone": payload.get("phone"),
        "call_time": payload.get("call_time"),
    }


def fail_superseded_queued_jobs(
    extension: str,
    phone: str,
    *,
    except_job_id: str,
    call_time: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int:
    """Drop older queued duplicate submits for the same call instance."""
    phone = (phone or "").strip()
    if not phone:
        return 0
    call_dt = _parse_call_time_iso(call_time or "")
    now = _now_iso()
    count = 0
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json FROM kommo_call_jobs
            WHERE extension = ?
              AND status IN ('queued', 'waiting_recording')
              AND id != ?
            """,
            (extension, except_job_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if (payload.get("phone") or "").strip() != phone:
                continue
            if call_dt is not None:
                other_dt = _parse_call_time_iso(str(payload.get("call_time") or ""))
                if other_dt is None:
                    continue
                if not _is_duplicate_call_submit(
                    call_dt=call_dt,
                    other_dt=other_dt,
                    session_id=str(session_id or ""),
                    other_session=str(payload.get("session_id") or ""),
                ):
                    continue
            conn.execute(
                """
                UPDATE kommo_call_jobs
                SET status = 'failed',
                    reason = 'Superseded by newer call',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            count += 1
        conn.commit()
    return count


def fail_weaker_duplicate_jobs(
    extension: str,
    phone: str,
    call_time: str,
    except_job_id: str,
    *,
    incoming_was_answered: bool,
    window_sec: int = 120,
) -> int:
    """Drop unanswered jobs when another client (web vs mobile) reports the same call answered.

    Session ids differ between clients, so this matches on phone + call time only.
    """
    if not incoming_was_answered:
        return 0
    phone = (phone or "").strip()
    call_dt = _parse_call_time_iso(call_time)
    if not phone or call_dt is None:
        return 0
    now = _now_iso()
    count = 0
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json FROM kommo_call_jobs
            WHERE extension = ?
              AND status IN ('queued', 'waiting_recording')
              AND id != ?
            """,
            (extension, except_job_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if payload.get("was_answered"):
                continue
            if not phones_match_for_dedup(payload.get("phone"), phone):
                continue
            other_dt = _parse_call_time_iso(str(payload.get("call_time") or ""))
            if other_dt is None or abs((other_dt - call_dt).total_seconds()) > window_sec:
                continue
            conn.execute(
                """
                UPDATE kommo_call_jobs
                SET status = 'failed',
                    reason = 'Superseded by answered report from another client',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            count += 1
        conn.commit()
    return count


def _linkedid_from_job_dict(job: dict[str, Any]) -> Optional[str]:
    payload = job.get("payload") or {}
    lid = payload.get("pbx_linkedid")
    if lid:
        return str(lid).strip() or None
    for path in (job.get("recording_path"), payload.get("recording_path")):
        if not path:
            continue
        name = Path(str(path)).stem
        if name.startswith("mikopbx_"):
            return name[len("mikopbx_") :] or None
    return None


def try_claim_pbx_linkedid(
    linkedid: str,
    job_id: str,
    extension: str,
    *,
    phone: Optional[str] = None,
    session_id: Optional[str] = None,
    call_time: Optional[str] = None,
) -> bool:
    """Atomically reserve a PBX linkedid for one Kommo job (prevents duplicate recordings)."""
    lid = (linkedid or "").strip()
    if not lid:
        return False
    now = _now_iso()
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO kommo_pbx_linkedid_claims
                    (linkedid, job_id, extension, phone, session_id, call_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (lid, job_id, extension, phone, session_id, call_time, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Re-claim by the same job must succeed: a retried job re-resolves the
            # same linkedid on every run (otherwise the CDR "disappears" after retry 1).
            row = conn.execute(
                "SELECT job_id FROM kommo_pbx_linkedid_claims WHERE linkedid = ?",
                (lid,),
            ).fetchone()
            return bool(row and row["job_id"] == job_id)


def release_pbx_linkedid_claim(linkedid: str, job_id: str) -> bool:
    """Release a claim when the matched recording was rejected (wrong CDR leg)."""
    lid = (linkedid or "").strip()
    if not lid:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM kommo_pbx_linkedid_claims
            WHERE linkedid = ? AND job_id = ?
            """,
            (lid, job_id),
        )
        conn.commit()
        return cur.rowcount > 0


def release_pbx_linkedid_claims_for_job(job_id: str) -> int:
    """Drop all PBX linkedid reservations held by one job (e.g. on failure)."""
    jid = (job_id or "").strip()
    if not jid:
        return 0
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM kommo_pbx_linkedid_claims WHERE job_id = ?",
            (jid,),
        )
        conn.commit()
        return cur.rowcount


def get_pbx_linkedid_owner(linkedid: str) -> Optional[dict[str, Any]]:
    lid = (linkedid or "").strip()
    if not lid:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM kommo_pbx_linkedid_claims WHERE linkedid = ?",
            (lid,),
        ).fetchone()
    if not row:
        return None
    return {
        "linkedid": row["linkedid"],
        "job_id": row["job_id"],
        "extension": row["extension"],
        "phone": row["phone"],
        "session_id": row["session_id"],
        "call_time": row["call_time"],
        "created_at": row["created_at"],
    }


def list_used_pbx_linkedids(
    extension: str,
    *,
    hours: int = 48,
    exclude_job_id: Optional[str] = None,
    phone: Optional[str] = None,
) -> set[str]:
    """PBX linkedids already claimed or uploaded — must not be reused for another call."""
    phone_norm = (phone or "").strip()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    used: set[str] = set()
    with _connect() as conn:
        claim_rows = conn.execute(
            """
            SELECT linkedid, job_id FROM kommo_pbx_linkedid_claims
            WHERE extension = ? AND created_at >= ?
            """,
            (extension, cutoff),
        ).fetchall()
        job_rows = conn.execute(
            """
            SELECT id, payload_json, recording_path, status FROM kommo_call_jobs
            WHERE extension = ?
              AND created_at >= ?
              AND status IN ('uploaded', 'processing', 'queued', 'waiting_recording')
            """,
            (extension, cutoff),
        ).fetchall()
    for row in claim_rows:
        if exclude_job_id and row["job_id"] == exclude_job_id:
            continue
        if row["linkedid"]:
            used.add(str(row["linkedid"]))
    for row in job_rows:
        if exclude_job_id and row["id"] == exclude_job_id:
            continue
        job = {
            "payload": json.loads(row["payload_json"] or "{}"),
            "recording_path": row["recording_path"],
        }
        lid = _linkedid_from_job_dict(job)
        if lid:
            job_phone = (job["payload"].get("phone") or "").strip()
            note_only_upload = (
                row["status"] == "uploaded"
                and not (row["recording_path"] or "").strip()
            )
            if phone_norm and job_phone == phone_norm and note_only_upload:
                continue
            used.add(lid)
    return used


def find_miko_recording_sibling_job(
    extension: str,
    phone: str,
    *,
    exclude_job_id: str,
    hours: int = 72,
) -> Optional[dict[str, Any]]:
    """Find another job for the same call that already matched Miko CDR/recording."""
    phone_norm = (phone or "").strip()
    ext = (extension or "").strip()
    if not phone_norm or not ext:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM kommo_call_jobs
            WHERE extension = ?
              AND created_at >= ?
              AND id != ?
              AND status = 'uploaded'
            ORDER BY updated_at DESC
            """,
            (ext, cutoff, exclude_job_id),
        ).fetchall()
    for row in rows:
        job = _row_to_dict(row)
        payload = job.get("payload") or {}
        if (payload.get("phone") or "").strip() != phone_norm:
            continue
        if job.get("upload_source") == "miko_pbx" or _linkedid_from_job_dict(job):
            return job
    return None


def cleanup_old_jobs(days: int = 7) -> int:
    """Remove Kommo call jobs and related rows older than *days* (default 7)."""
    days = max(1, int(days))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, recording_path FROM kommo_call_jobs WHERE created_at < ?",
            (cutoff,),
        ).fetchall()
        job_ids = [str(r["id"]) for r in rows if r["id"]]
        for row in rows:
            _remove_job_recording_file(row["recording_path"])
        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            conn.execute(
                f"DELETE FROM kommo_pbx_linkedid_claims WHERE job_id IN ({placeholders})",
                job_ids,
            )
            conn.execute(
                f"DELETE FROM kommo_recording_hashes WHERE job_id IN ({placeholders})",
                job_ids,
            )
        conn.execute(
            "DELETE FROM kommo_pbx_linkedid_claims WHERE created_at < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM kommo_recording_hashes WHERE created_at < ?",
            (cutoff,),
        )
        cur = conn.execute(
            "DELETE FROM kommo_call_jobs WHERE created_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def _remove_job_recording_file(recording_path: Optional[str]) -> None:
    if not recording_path:
        return
    try:
        p = Path(recording_path)
        if p.is_file():
            p.unlink(missing_ok=True)
        parent = p.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def reset_job_for_retry(job_id: str) -> Optional[dict[str, Any]]:
    """Requeue a finished/failed job so workers can fetch PBX recording again."""
    return reset_job_for_reupload(job_id)


def reset_job_for_reupload(
    job_id: str,
    *,
    lead_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Requeue any job (including successful uploads) for admin re-upload."""
    job = get_job(job_id)
    if not job:
        return None
    payload = dict(job.get("payload") or {})
    preserved_linkedid = _linkedid_from_job_dict(job) or (payload.get("pbx_linkedid") or "").strip()
    payload.pop("retry_after", None)
    payload.pop("pbx_linkedid", None)
    payload.pop("recording_sha256", None)
    payload["pbx_recording_retry"] = 0
    payload["admin_reupload"] = True
    if preserved_linkedid:
        payload["pbx_linkedid"] = preserved_linkedid
    if lead_id and int(lead_id) > 0:
        payload["lead_id"] = int(lead_id)
    with _connect() as conn:
        conn.execute(
            "DELETE FROM kommo_pbx_linkedid_claims WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
    clear_recording_hash_for_job(job_id)
    update_fields: dict[str, Any] = {
        "status": "queued",
        "reason": None,
        "upload_source": None,
        "recording_path": None,
        "payload_json": payload,
    }
    if lead_id and int(lead_id) > 0:
        update_fields["lead_id"] = int(lead_id)
    return update_job(job_id, **update_fields)


def get_job_recording_lead_id(job_id: str) -> Optional[int]:
    """Backfill lead_id from recording-hash registry (older uploads)."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT lead_id FROM kommo_recording_hashes
            WHERE job_id = ? AND lead_id IS NOT NULL AND lead_id > 0
            LIMIT 1
            """,
            (jid,),
        ).fetchone()
    if not row or row["lead_id"] is None:
        return None
    try:
        return int(row["lead_id"])
    except (TypeError, ValueError):
        return None


def clear_recording_hash_for_job(job_id: str) -> int:
    """Remove audio dedup rows so admin/client retry can upload the same file again."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM kommo_recording_hashes WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount


def count_call_jobs_admin(
    *,
    hours: int = 72,
    extension: Optional[str] = None,
    status: Optional[str] = None,
    phone: Optional[str] = None,
) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    clauses = ["created_at >= ?"]
    params: list[Any] = [cutoff]
    if extension:
        clauses.append("extension = ?")
        params.append(extension.strip())
    if status:
        clauses.append("status = ?")
        params.append(status.strip())
    if phone:
        clauses.append("json_extract(payload_json, '$.phone') LIKE ?")
        params.append(f"%{phone.strip()}%")
    where = " AND ".join(clauses)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM kommo_call_jobs WHERE {where}",
            params,
        ).fetchone()
    return int(row["cnt"]) if row else 0


def list_call_jobs_admin(
    *,
    hours: int = 72,
    extension: Optional[str] = None,
    status: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """All Kommo jobs for admin call log (any status)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    clauses = ["created_at >= ?"]
    params: list[Any] = [cutoff]
    if extension:
        clauses.append("extension = ?")
        params.append(extension.strip())
    if status:
        clauses.append("status = ?")
        params.append(status.strip())
    if phone:
        clauses.append("json_extract(payload_json, '$.phone') LIKE ?")
        params.append(f"%{phone.strip()}%")
    where = " AND ".join(clauses)
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    params.extend([limit, offset])
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM kommo_call_jobs
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "id": row["id"],
        "extension": row["extension"],
        "dedup_key": row["dedup_key"],
        "payload": payload,
        "status": row["status"],
        "lead_id": row["lead_id"],
        "contact_id": row["contact_id"] if "contact_id" in row.keys() else None,
        "crm_entity": row["crm_entity"] if "crm_entity" in row.keys() else None,
        "upload_source": row["upload_source"],
        "reason": row["reason"],
        "recording_path": row["recording_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def is_cdr_entity_processed(linkedid: str) -> bool:
    lid = (linkedid or "").strip()
    if not lid:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM kommo_cdr_entity_processed WHERE linkedid = ? LIMIT 1",
            (lid,),
        ).fetchone()
    return row is not None


def clear_cdr_entity_processed(linkedid: str) -> bool:
    """Remove processed marker so entity rules can run again (manual retry)."""
    lid = (linkedid or "").strip()
    if not lid:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM kommo_cdr_entity_processed WHERE linkedid = ?",
            (lid,),
        )
        conn.commit()
    return cur.rowcount > 0


def mark_cdr_entity_processed(
    linkedid: str,
    *,
    phone: str = "",
    extension: str = "",
    call_start: str = "",
    source: str = "cdr_worker",
    rule_id: Optional[int] = None,
    task_created: bool = False,
    task_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    lead_id: Optional[int] = None,
) -> None:
    lid = (linkedid or "").strip()
    if not lid:
        return
    now = _now_iso()
    tid = int(task_id) if task_id else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kommo_cdr_entity_processed
                (linkedid, phone, extension, call_start, source, rule_id,
                 task_created, task_id, contact_id, lead_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(linkedid) DO UPDATE SET
                source = excluded.source,
                rule_id = excluded.rule_id,
                task_created = excluded.task_created,
                task_id = COALESCE(excluded.task_id, kommo_cdr_entity_processed.task_id),
                contact_id = excluded.contact_id,
                lead_id = excluded.lead_id,
                created_at = excluded.created_at
            """,
            (
                lid,
                (phone or "").strip(),
                (extension or "").strip(),
                (call_start or "").strip(),
                (source or "cdr_worker").strip(),
                rule_id,
                1 if task_created else 0,
                tid,
                contact_id,
                lead_id,
                now,
            ),
        )
        conn.commit()


def map_cdr_entity_processed(hours: int = 168) -> dict[str, dict[str, Any]]:
    """linkedid -> entity rule result row for admin inbound call log."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT linkedid, phone, extension, call_start, source, rule_id,
                   task_created, task_id, contact_id, lead_id, created_at
            FROM kommo_cdr_entity_processed
            WHERE created_at >= ?
            """,
            (cutoff,),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        lid = (row["linkedid"] or "").strip()
        if not lid:
            continue
        out[lid] = {
            "linkedid": lid,
            "phone": row["phone"] or "",
            "extension": row["extension"] or "",
            "call_start": row["call_start"] or "",
            "source": row["source"] or "",
            "rule_id": row["rule_id"],
            "task_created": bool(row["task_created"]),
            "task_id": row["task_id"],
            "contact_id": row["contact_id"],
            "lead_id": row["lead_id"],
            "created_at": row["created_at"] or "",
        }
    return out


def map_incoming_jobs_by_linkedid(hours: int = 168) -> dict[str, dict[str, Any]]:
    """Map pbx linkedid -> latest incoming kommo_call_jobs row."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM kommo_call_jobs
            WHERE created_at >= ?
              AND CAST(json_extract(payload_json, '$.is_incoming') AS INTEGER) = 1
            ORDER BY created_at DESC
            """,
            (cutoff,),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        job = _row_to_dict(row)
        payload = job.get("payload") or {}
        lid = (payload.get("pbx_linkedid") or "").strip()
        if not lid or lid in out:
            continue
        out[lid] = job
    return out


def cdr_call_has_active_kommo_job(
    *,
    linkedid: str,
    phone: str,
    call_time_utc: datetime,
    window_seconds: int = 600,
) -> bool:
    """True when a softphone process-call job already covers this CDR call."""
    lid = (linkedid or "").strip()
    if lid:
        if get_pbx_linkedid_owner(lid):
            return True
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM kommo_call_jobs
                WHERE json_extract(payload_json, '$.pbx_linkedid') = ?
                LIMIT 1
                """,
                (lid,),
            ).fetchone()
            if row:
                return True

    phone = (phone or "").strip()
    if not phone:
        return False

    win = max(60, int(window_seconds))
    lo = (call_time_utc - timedelta(seconds=win)).isoformat()
    hi = (call_time_utc + timedelta(seconds=win)).isoformat()
    active = ("queued", "waiting_recording", "processing", "uploaded")
    placeholders = ",".join("?" for _ in active)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT 1 FROM kommo_call_jobs
            WHERE json_extract(payload_json, '$.phone') = ?
              AND json_extract(payload_json, '$.is_incoming') = 1
              AND status IN ({placeholders})
              AND json_extract(payload_json, '$.call_time') >= ?
              AND json_extract(payload_json, '$.call_time') <= ?
            LIMIT 1
            """,
            (phone, *active, lo, hi),
        ).fetchone()
    return row is not None
