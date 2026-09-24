"""Resolve Miko PBX recordings for Kommo upload jobs."""

from __future__ import annotations

import logging
import asyncio
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

log = logging.getLogger("kommo_recording")

PBX_CDR_RETRY_DELAYS = [0, 2, 5, 8]
CDR_LOOKUP_DELAYS = [0]
MAX_PBX_RECORDING_JOB_RETRIES = 6
JOB_RETRY_WAITS_SEC = [3, 5, 8, 12, 15, 20]
MAX_START_DIFF_SECONDS = 180
MAX_RECORDING_START_DIFF_SECONDS = 55
_COMMON_UTC_OFFSETS = (0, 3, 4, 2, 5, -5, -4, -6, 1, -3)

# Kommo missed-call status (same as kommo_crm.AMO_MISSED_CALL_STATUS)
_KOMMO_MISSED_STATUS = 6

_pbx_utc_offset_hours: float = 0.0


@dataclass
class CdrCallInfo:
    linkedid: str
    disposition: str
    billsec: int
    duration: int
    has_recording: bool
    caller_id: Optional[str]
    was_answered: bool
    kommo_call_result: str
    kommo_call_status: Optional[int]


@dataclass
class PbxCallResolution:
    recording_path: Optional[str]
    cdr_duration: Optional[int]
    cdr_info: Optional[CdrCallInfo]


def configure_pbx_timezone(offset_hours: float) -> None:
    global _pbx_utc_offset_hours
    _pbx_utc_offset_hours = float(offset_hours or 0)


def _pbx_local_sql_window(call_time: datetime) -> tuple[str, str]:
    """Build start/end for CDR SQL — Miko stores naive local wall clock."""
    ct = call_time if call_time.tzinfo else call_time.replace(tzinfo=timezone.utc)
    local = (ct + timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=None)
    start = local - timedelta(hours=2)
    end = local + timedelta(hours=2)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


async def wait_recording_file_stable(
    path: Path,
    *,
    billsec: int = 0,
    interval_sec: float = 1.0,
    max_wait_sec: float = 8.0,
) -> bool:
    """Return True once the PBX recording file exists, is non-empty and stops growing."""
    p = Path(path)
    waited = 0.0
    last_size = -1
    while True:
        try:
            size = p.stat().st_size if p.is_file() else 0
        except OSError:
            size = 0
        if size > 0 and size == last_size:
            return True
        if waited >= max_wait_sec:
            return size > 0
        last_size = size
        await asyncio.sleep(interval_sec)
        waited += interval_sec


def recording_file_duration_seconds(path: Optional[str]) -> Optional[int]:
    """Best-effort media duration for Kommo call-note sync (WAV from Miko PBX)."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    suffix = p.suffix.lower()
    try:
        if suffix == ".wav":
            with wave.open(str(p), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate()
                if rate <= 0:
                    return None
                return max(0, int(round(frames / float(rate))))
    except (OSError, wave.Error):
        return None
    return None


def find_downloaded_recording(work_dir: Path, linkedid: str) -> Optional[Path]:
    """Locate the copied Miko file (keeps original PBX filename, not mikopbx_*.mp3)."""
    del linkedid  # job work_dir is unique; any non-staging file is the recording
    if not work_dir.is_dir():
        return None
    candidates: list[Path] = []
    for candidate in work_dir.iterdir():
        if not candidate.is_file() or candidate.name.startswith(".download_"):
            continue
        try:
            if candidate.stat().st_size > 0:
                candidates.append(candidate)
        except OSError:
            continue
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda p: p.stat().st_mtime)


def is_recording_usable(path: Optional[str]) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def is_pbx_recording_acceptable(
    *,
    was_answered: bool,
    call_duration_sec: int,
    answer_time: Optional[datetime],
    call_time: datetime,
    pbx_path: Optional[str],
    cdr_duration_sec: Optional[int],
) -> tuple[bool, Optional[str]]:
    if not is_recording_usable(pbx_path):
        return False, "PBX file missing or empty"

    if not was_answered:
        return False, "PBX CDR not answered — skip ring-only recording"

    compare_duration = call_duration_sec
    if cdr_duration_sec and cdr_duration_sec > compare_duration:
        compare_duration = int(cdr_duration_sec)
    if was_answered and answer_time and call_duration_sec > 0:
        pre_answer = (answer_time - call_time).total_seconds()
        if 0 < pre_answer < call_duration_sec:
            compare_duration = max(1, call_duration_sec - int(round(pre_answer)))

    if cdr_duration_sec and cdr_duration_sec > 0 and compare_duration >= 3:
        diff = abs(cdr_duration_sec - compare_duration)
        tolerance = max(30, compare_duration // 2)
        if diff > tolerance:
            return False, (
                f"CDR duration {cdr_duration_sec}s vs talk ~{compare_duration}s "
                f"(diff {diff}s > tolerance {tolerance}s)"
            )

    if compare_duration < 5:
        return True, None

    try:
        pbx_bytes = Path(pbx_path).stat().st_size
        duration_for_size = cdr_duration_sec if cdr_duration_sec and cdr_duration_sec > 0 else compare_duration
        compressed = str(pbx_path).lower().endswith((".webm", ".mp3", ".ogg"))
        if compare_duration < 30:
            min_bps = 200 if compressed else 1500
            floor_bytes = 1500 if compressed else 8000
        else:
            min_bps = 400 if compressed else 4000
            floor_bytes = 4000 if compressed else 50_000
        min_bytes = max(floor_bytes, duration_for_size * min_bps)
        if pbx_bytes < min_bytes:
            return False, (
                f"PBX file too small ({pbx_bytes} bytes < min {min_bytes} "
                f"for ~{duration_for_size}s)"
            )
    except OSError:
        pass
    return True, None


def _parse_cdr_start(value: Any) -> Optional[datetime]:
    """Parse CDR start as naive local PBX wall clock (no timezone)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "T" in s:
        s = s.replace("T", " ", 1)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def _call_vs_cdr_diff_seconds(rec_time: datetime, call_time: datetime) -> float:
    """Seconds between UTC call_time (browser) and naive PBX-local CDR start."""
    ct = call_time.astimezone(timezone.utc) if call_time.tzinfo else call_time.replace(tzinfo=timezone.utc)
    if rec_time.tzinfo is not None:
        rt = rec_time.astimezone(timezone.utc)
        return abs((rt - ct).total_seconds())
    naive = rec_time
    if _pbx_utc_offset_hours:
        rt = (naive - timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=timezone.utc)
        return abs((rt - ct).total_seconds())
    return min(
        abs((naive - timedelta(hours=oh)).replace(tzinfo=timezone.utc) - ct).total_seconds()
        for oh in _COMMON_UTC_OFFSETS
    )


def _digits_match(a: str, b: str) -> bool:
    """True if two digit strings refer to the same phone (handles +1 / 10 vs 11 digits)."""
    da = "".join(c for c in a if c.isdigit())
    db = "".join(c for c in b if c.isdigit())
    if not da or not db:
        return False
    ra = da[-10:] if len(da) >= 10 else da
    rb = db[-10:] if len(db) >= 10 else db
    return ra == rb or da.endswith(rb) or db.endswith(ra)


def _is_trunk_leg(record: dict) -> bool:
    trunk = str(record.get("trunk") or record.get("to_account") or "").upper()
    return "TRUNK" in trunk or trunk.startswith("SIP-")


def _row_matches_phone(record: dict, phone: str, *, is_incoming: bool) -> bool:
    dst = str(record.get("dst") or record.get("dst_num") or "")
    src = str(record.get("src") or record.get("src_num") or "")
    if is_incoming:
        return _digits_match(src, phone) or _digits_match(dst, phone)
    return _digits_match(dst, phone) or _digits_match(src, phone)


def _int_field(record: dict, *keys: str) -> int:
    for key in keys:
        try:
            return int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def kommo_call_result_text(result_label: str, call_from_label: Optional[str]) -> str:
    caller = (
        f"Call from {call_from_label.strip()}"
        if call_from_label and call_from_label.strip()
        else None
    )
    if caller:
        return f"{result_label} · {caller}"
    return result_label


def disposition_to_kommo(
    disposition: str,
    billsec: int,
    *,
    call_from_label: Optional[str],
) -> tuple[bool, str, Optional[int]]:
    """Map PBX CDR disposition to Kommo call note fields."""
    disp = (disposition or "").upper()
    if disp == "ANSWERED":
        if billsec > 0:
            label = "Answered"
            return True, kommo_call_result_text(label, call_from_label), None
        label = "Answered (no talk time)"
        return True, kommo_call_result_text(label, call_from_label), None
    if disp == "BUSY":
        return False, kommo_call_result_text("Busy", call_from_label), _KOMMO_MISSED_STATUS
    if disp == "NO ANSWER":
        return False, kommo_call_result_text("No Answer", call_from_label), _KOMMO_MISSED_STATUS
    if disp == "CANCEL":
        if billsec > 0:
            return True, kommo_call_result_text("Answered", call_from_label), None
        return False, kommo_call_result_text("Cancelled", call_from_label), _KOMMO_MISSED_STATUS
    if disp == "FAILED":
        return False, kommo_call_result_text("Failed", call_from_label), _KOMMO_MISSED_STATUS
    return False, kommo_call_result_text("No Answer", call_from_label), _KOMMO_MISSED_STATUS


def rank_cdr_records_for_note(
    records: list[dict],
    *,
    phone: str,
    call_time: datetime,
    is_incoming: bool,
    exclude_linkedids: Optional[set[str]] = None,
    call_duration_sec: Optional[int] = None,
    call_end_time: Optional[datetime] = None,
    require_recording_match: bool = False,
) -> list[dict]:
    """Rank CDR rows for Kommo sync (best time match first, skip already-used linkedids)."""
    if call_time.tzinfo is None:
        call_time = call_time.replace(tzinfo=timezone.utc)

    excluded = exclude_linkedids or set()
    max_start_diff = (
        MAX_RECORDING_START_DIFF_SECONDS if require_recording_match else MAX_START_DIFF_SECONDS
    )
    scored: list[tuple[float, dict]] = []

    for r in records:
        linkedid = str(r.get("linkedid") or r.get("linked_id") or "")
        if not linkedid or linkedid in excluded:
            continue
        rec_time = _parse_cdr_start(r.get("start") or r.get("calldate"))
        if not rec_time:
            continue
        diff = _call_vs_cdr_diff_seconds(rec_time, call_time)
        if diff > max_start_diff:
            continue
        if not _row_matches_phone(r, phone, is_incoming=is_incoming):
            continue

        billsec = _int_field(r, "billsec", "duration")
        if require_recording_match and call_duration_sec and call_duration_sec > 0 and billsec > 0:
            dur_diff = abs(billsec - call_duration_sec)
            tolerance = max(12, min(30, call_duration_sec // 3))
            if dur_diff > tolerance and diff > 25:
                continue

        score = diff
        if not is_incoming and _is_trunk_leg(r):
            score -= 60
        if billsec > 0:
            score -= min(billsec, 30)
        if call_duration_sec and call_duration_sec > 0 and billsec > 0:
            score += abs(billsec - call_duration_sec) * 0.75
        if call_end_time is not None:
            try:
                end_utc = (
                    call_end_time.astimezone(timezone.utc)
                    if call_end_time.tzinfo
                    else call_end_time.replace(tzinfo=timezone.utc)
                )
                rt = rec_time
                if rt.tzinfo is None:
                    if _pbx_utc_offset_hours:
                        rt = (rt - timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=timezone.utc)
                    else:
                        rt = rt.replace(tzinfo=timezone.utc)
                else:
                    rt = rt.astimezone(timezone.utc)
                if rt > end_utc + timedelta(seconds=45):
                    continue
            except (TypeError, ValueError, OverflowError):
                pass

        scored.append((score, r))

    scored.sort(key=lambda item: item[0])
    return [r for _, r in scored]


def pick_best_cdr_record_for_note(
    records: list[dict],
    *,
    phone: str,
    call_time: datetime,
    is_incoming: bool,
    exclude_linkedids: Optional[set[str]] = None,
    call_duration_sec: Optional[int] = None,
) -> Optional[dict]:
    """Pick CDR row for Kommo note sync (no recording required, any disposition)."""
    ranked = rank_cdr_records_for_note(
        records,
        phone=phone,
        call_time=call_time,
        is_incoming=is_incoming,
        exclude_linkedids=exclude_linkedids,
        call_duration_sec=call_duration_sec,
    )
    return ranked[0] if ranked else None


def summarize_cdr_linkedid(
    records: list[dict],
    linkedid: str,
    *,
    phone: str,
    is_incoming: bool,
    call_from_label: Optional[str],
) -> CdrCallInfo:
    """Aggregate all CDR legs for one linkedid into Kommo-ready call metadata."""
    rows = [
        r
        for r in records
        if (r.get("linkedid") or r.get("linked_id")) == linkedid
    ]
    if not rows:
        rows = []

    phone_rows = [r for r in rows if _row_matches_phone(r, phone, is_incoming=is_incoming)]
    trunk_rows = [r for r in phone_rows if _is_trunk_leg(r)] if not is_incoming else []
    primary_candidates = trunk_rows or phone_rows or rows
    primary = primary_candidates[0] if primary_candidates else {}

    has_recording = any(str(r.get("recording") or "").strip() for r in rows)
    billsec = max((_int_field(r, "billsec") for r in phone_rows or rows), default=0)
    duration = max((_int_field(r, "duration") for r in phone_rows or rows), default=0)

    disposition = (primary.get("disposition") or "").upper()
    for r in phone_rows or rows:
        disp = (r.get("disposition") or "").upper()
        bs = _int_field(r, "billsec")
        if disp == "ANSWERED" and bs > 0:
            disposition = "ANSWERED"
            billsec = max(billsec, bs)
            break

    if not disposition and rows:
        disposition = (rows[0].get("disposition") or "NO ANSWER").upper()

    caller_id = (
        str(primary.get("caller_id") or "").strip()
        or str(primary.get("src_num") or "").strip()
        or None
    )
    if caller_id and not any(c.isdigit() for c in caller_id):
        caller_id = None

    label = call_from_label or caller_id
    was_answered, kommo_result, kommo_status = disposition_to_kommo(
        disposition, billsec, call_from_label=label
    )

    if was_answered and not has_recording and billsec > 0:
        kommo_result = kommo_call_result_text("Answered · no PBX recording", label)

    return CdrCallInfo(
        linkedid=linkedid,
        disposition=disposition or "UNKNOWN",
        billsec=billsec,
        duration=duration,
        has_recording=has_recording,
        caller_id=caller_id,
        was_answered=was_answered,
        kommo_call_result=kommo_result,
        kommo_call_status=kommo_status,
    )


def pick_best_cdr_record(
    records: list[dict],
    *,
    phone: str,
    call_time: datetime,
    was_answered: bool,
    call_duration_sec: Optional[int],
) -> Optional[dict]:
    if call_time.tzinfo is None:
        call_time = call_time.replace(tzinfo=timezone.utc)

    best: Optional[dict] = None
    best_diff = float("inf")
    phone_digits = "".join(c for c in phone if c.isdigit())

    for r in records:
        if not (r.get("recording") or r.get("linkedid")):
            continue
        rec_time = _parse_cdr_start(r.get("start") or r.get("calldate"))
        if not rec_time:
            continue
        diff = _call_vs_cdr_diff_seconds(rec_time, call_time)
        if diff > MAX_START_DIFF_SECONDS:
            continue
        if was_answered:
            disp = (r.get("disposition") or "").upper()
            if disp and disp != "ANSWERED":
                continue
        if was_answered and call_duration_sec:
            try:
                cdr_dur = int(r.get("billsec") or r.get("duration") or 0)
            except (TypeError, ValueError):
                cdr_dur = 0
            if cdr_dur > 0:
                dur_diff = abs(cdr_dur - call_duration_sec)
                tolerance = max(30, call_duration_sec // 2)
                if dur_diff > tolerance:
                    continue
        dst = str(r.get("dst") or r.get("dst_num") or "")
        src = str(r.get("src") or r.get("src_num") or "")
        if phone_digits:
            if not _digits_match(dst, phone) and not _digits_match(src, phone):
                continue
        if diff < best_diff:
            best_diff = diff
            best = r
    return best


async def _query_cdr_records(
    query_cdr: Callable[..., Any],
    extension: str,
    call_time: Optional[datetime] = None,
    *,
    wide: bool = False,
) -> list[dict]:
    kwargs: dict[str, Any] = {"limit": 100}
    if not wide:
        kwargs["ext"] = extension
    if call_time is not None:
        kwargs["start_from"], kwargs["start_to"] = _pbx_local_sql_window(call_time)
    label = "wide CDR" if wide else "CDR"
    try:
        records = await query_cdr(**kwargs)
    except Exception as exc:
        log.warning("%s query failed: %s", label, exc)
        print(f"[kommo_recording] {label} query failed: {exc}", flush=True)
        return []
    if isinstance(records, dict):
        records = records.get("data") or records.get("result") or []
    if not isinstance(records, list):
        return []
    return records


async def _list_verified_cdr_candidates(
    *,
    query_cdr: Callable[..., Any],
    verify_linkedid: Optional[Callable[..., Union[bool, Awaitable[bool]]]],
    extension: str,
    phone: str,
    call_time: datetime,
    is_incoming: bool,
    exclude_linkedids: Optional[set[str]] = None,
    call_duration_sec: Optional[int] = None,
    call_end_time: Optional[datetime] = None,
    log_attempt: bool = False,
    require_recording_match: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Merge narrow + wide CDR queries, return verified candidates best-first."""

    async def _verify(linked_id: str) -> bool:
        if verify_linkedid is None:
            return True
        try:
            ok = verify_linkedid(linked_id, extension)
            if asyncio.iscoroutine(ok):
                ok = await ok
            return bool(ok)
        except Exception as exc:
            log.warning("verify_linkedid failed for %s: %s", linked_id, exc)
            return False

    record_batches: list[list[dict]] = []
    if not is_incoming:
        wide_records = await _query_cdr_records(query_cdr, extension, call_time, wide=True)
        if wide_records:
            record_batches.append(wide_records)
    narrow_records = await _query_cdr_records(query_cdr, extension, call_time)
    if narrow_records:
        record_batches.append(narrow_records)

    combined: list[dict] = []
    seen_row: set[str] = set()
    for batch in record_batches:
        for row in batch:
            key = str(row.get("linkedid") or row.get("linked_id") or id(row))
            if key in seen_row:
                continue
            seen_row.add(key)
            combined.append(row)

    if log_attempt:
        window = _pbx_local_sql_window(call_time)
        print(
            f"[kommo_recording] CDR candidates ext={extension} phone={phone} "
            f"window={window[0]}..{window[1]} rows={len(combined)} "
            f"excluded_linkedids={len(exclude_linkedids or ())}",
            flush=True,
        )

    ranked = rank_cdr_records_for_note(
        combined,
        phone=phone,
        call_time=call_time,
        is_incoming=is_incoming,
        exclude_linkedids=exclude_linkedids,
        call_duration_sec=call_duration_sec,
        call_end_time=call_end_time,
        require_recording_match=require_recording_match,
    )

    verified: list[dict] = []
    for candidate in ranked:
        linked_id = str(candidate.get("linkedid") or candidate.get("linked_id") or "")
        if not linked_id:
            continue
        if not await _verify(linked_id):
            if log_attempt:
                print(
                    f"[kommo_recording] skip linkedid={linked_id} (not owned by ext={extension})",
                    flush=True,
                )
            continue
        verified.append(candidate)

    return verified, combined


async def _pick_cdr_for_note(
    *,
    query_cdr: Callable[..., Any],
    verify_linkedid: Optional[Callable[..., Union[bool, Awaitable[bool]]]],
    extension: str,
    phone: str,
    call_time: datetime,
    is_incoming: bool,
    log_attempt: bool,
    exclude_linkedids: Optional[set[str]] = None,
    call_duration_sec: Optional[int] = None,
) -> tuple[Optional[dict], list[dict]]:
    """Find best CDR row for Kommo note; wide query fallback for originate trunk legs."""

    async def _pick_from_records(records: list[dict]) -> Optional[dict]:
        ranked = rank_cdr_records_for_note(
            records,
            phone=phone,
            call_time=call_time,
            is_incoming=is_incoming,
            exclude_linkedids=exclude_linkedids,
            call_duration_sec=call_duration_sec,
        )
        for candidate in ranked:
            linked_id = str(candidate.get("linkedid") or candidate.get("linked_id") or "")
            if not linked_id:
                continue
            if verify_linkedid is not None:
                try:
                    ok = verify_linkedid(linked_id, extension)
                    if asyncio.iscoroutine(ok):
                        ok = await ok
                except Exception as exc:
                    log.warning("verify_linkedid failed for %s: %s", linked_id, exc)
                    ok = False
                if not ok:
                    if log_attempt:
                        print(
                            f"[kommo_recording] CDR pick linkedid={linked_id} rejected "
                            f"(not owned by ext={extension})",
                            flush=True,
                        )
                    continue
            return candidate
        return None

    async def _try_wide() -> tuple[Optional[dict], list[dict]]:
        wide_records = await _query_cdr_records(query_cdr, extension, call_time, wide=True)
        if not wide_records:
            return None, []
        wide_best = await _pick_from_records(wide_records)
        if not wide_best:
            if log_attempt:
                print(
                    f"[kommo_recording] wide CDR query rows={len(wide_records)} — no usable match "
                    f"(excluded={len(exclude_linkedids or ())})",
                    flush=True,
                )
            return None, wide_records
        linked_id = str(wide_best.get("linkedid") or wide_best.get("linked_id") or "")
        if log_attempt:
            print(
                f"[kommo_recording] wide CDR matched linkedid={linked_id} rows={len(wide_records)}",
                flush=True,
            )
        return wide_best, wide_records

    # Originate/outbound: recording is usually on the trunk leg (outside ext filter).
    if not is_incoming:
        wide_best, wide_records = await _try_wide()
        if wide_best:
            return wide_best, wide_records

    all_records = await _query_cdr_records(query_cdr, extension, call_time)
    if log_attempt:
        window = _pbx_local_sql_window(call_time)
        print(
            f"[kommo_recording] CDR query ext={extension} phone={phone} "
            f"window={window[0]}..{window[1]} rows={len(all_records)} "
            f"pbx_utc_offset={_pbx_utc_offset_hours} excluded_linkedids={len(exclude_linkedids or ())}",
            flush=True,
        )

    best = await _pick_from_records(all_records)
    if best:
        return best, all_records

    if log_attempt and all_records:
        print(
            f"[kommo_recording] CDR note pick miss phone={phone} ext={extension} "
            f"(rows with linkedid={sum(1 for r in all_records if r.get('linkedid'))})",
            flush=True,
        )

    wide_best, wide_records = await _try_wide()
    if wide_best:
        return wide_best, wide_records
    return None, all_records


async def resolve_pbx_call(
    *,
    query_cdr: Callable[..., Any],
    download_recording: Callable[[str, Path], Any],
    extension: str,
    phone: str,
    call_time: datetime,
    is_incoming: bool,
    was_answered: bool,
    call_duration_sec: int,
    call_from_label: Optional[str] = None,
    answer_time: Optional[datetime] = None,
    call_end_time: Optional[datetime] = None,
    work_dir: Optional[Path] = None,
    verify_linkedid: Optional[Callable[..., Union[bool, Awaitable[bool]]]] = None,
    exclude_linkedids: Optional[set[str]] = None,
    claim_linkedid: Optional[Callable[[str], bool]] = None,
    release_linkedid: Optional[Callable[[str], None]] = None,
) -> PbxCallResolution:
    """Resolve PBX CDR for Kommo: recording file and/or call note metadata."""

    work_dir = work_dir or Path(tempfile.gettempdir()) / "callspire_kommo_recordings"
    work_dir.mkdir(parents=True, exist_ok=True)

    for attempt, delay in enumerate(CDR_LOOKUP_DELAYS):
        if delay > 0:
            await asyncio.sleep(delay)
            print(
                f"[kommo_recording] CDR lookup retry {attempt} for {phone} ext={extension}",
                flush=True,
            )

        candidates, all_records = await _list_verified_cdr_candidates(
            query_cdr=query_cdr,
            verify_linkedid=verify_linkedid,
            extension=extension,
            phone=phone,
            call_time=call_time,
            is_incoming=is_incoming,
            exclude_linkedids=exclude_linkedids,
            call_duration_sec=call_duration_sec or None,
            call_end_time=call_end_time,
            log_attempt=(attempt == 0),
            require_recording_match=True,
        )
        if not candidates:
            candidates, all_records = await _list_verified_cdr_candidates(
                query_cdr=query_cdr,
                verify_linkedid=verify_linkedid,
                extension=extension,
                phone=phone,
                call_time=call_time,
                is_incoming=is_incoming,
                exclude_linkedids=exclude_linkedids,
                call_duration_sec=call_duration_sec or None,
                call_end_time=call_end_time,
                log_attempt=False,
                require_recording_match=False,
            )
        if not candidates:
            continue

        for best in candidates:
            linked_id = str(best.get("linkedid") or best.get("linked_id") or "")
            if not linked_id:
                continue

            claimed = True
            if claim_linkedid is not None:
                claimed = bool(claim_linkedid(linked_id))
                if not claimed:
                    owner_hint = ""
                    print(
                        f"[kommo_recording] skip linkedid={linked_id} — already claimed by another job{owner_hint}",
                        flush=True,
                    )
                    continue

            cdr_info = summarize_cdr_linkedid(
                all_records,
                linked_id,
                phone=phone,
                is_incoming=is_incoming,
                call_from_label=call_from_label,
            )
            print(
                f"[kommo_recording] CDR matched linkedid={linked_id} "
                f"disposition={cdr_info.disposition} billsec={cdr_info.billsec} "
                f"has_recording={cdr_info.has_recording} result={cdr_info.kommo_call_result}",
                flush=True,
            )

            if not cdr_info.was_answered:
                return PbxCallResolution(
                    None,
                    cdr_info.billsec or cdr_info.duration or None,
                    cdr_info,
                )
            if not cdr_info.has_recording and int(cdr_info.billsec or 0) < 3:
                return PbxCallResolution(
                    None,
                    cdr_info.billsec or cdr_info.duration or None,
                    cdr_info,
                )

            recording_ok = False
            for rec_attempt, rec_delay in enumerate(PBX_CDR_RETRY_DELAYS):
                if rec_delay > 0:
                    await asyncio.sleep(rec_delay)
                    if rec_attempt > 0:
                        msg = (
                            f"[kommo_recording] recording download retry {rec_attempt} "
                            f"linkedid={cdr_info.linkedid}"
                        )
                        log.info(msg)
                        print(msg, flush=True)

                safe_id = cdr_info.linkedid.replace("/", "_").replace("\\", "_")
                dest_hint = work_dir / f".download_{safe_id}"
                try:
                    ok = await download_recording(cdr_info.linkedid, dest_hint)
                except Exception as exc:
                    log.warning("recording download failed: %s", exc)
                    print(
                        f"[kommo_recording] download failed linkedid={cdr_info.linkedid}: {exc}",
                        flush=True,
                    )
                    continue
                dest = find_downloaded_recording(work_dir, cdr_info.linkedid)
                if not ok or dest is None:
                    print(
                        f"[kommo_recording] download empty linkedid={cdr_info.linkedid}",
                        flush=True,
                    )
                    continue

                cdr_duration = cdr_info.billsec or cdr_info.duration or None
                file_duration = recording_file_duration_seconds(str(dest))
                compare_duration = call_duration_sec
                if file_duration and file_duration > compare_duration:
                    compare_duration = file_duration
                acceptable, reason = is_pbx_recording_acceptable(
                    was_answered=bool(cdr_info.was_answered),
                    call_duration_sec=compare_duration,
                    answer_time=answer_time,
                    call_time=call_time,
                    pbx_path=str(dest),
                    cdr_duration_sec=file_duration or cdr_duration,
                )
                if acceptable:
                    note_duration = file_duration or cdr_duration
                    print(
                        f"[kommo_recording] recording ready linkedid={cdr_info.linkedid} "
                        f"path={dest.name} bytes={dest.stat().st_size} "
                        f"file_sec={file_duration} billsec={cdr_duration}",
                        flush=True,
                    )
                    return PbxCallResolution(str(dest), note_duration, cdr_info)

                log.info("PBX recording rejected: %s", reason)
                print(
                    f"[kommo_recording] rejected linkedid={cdr_info.linkedid}: {reason}",
                    flush=True,
                )
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass

            if cdr_info.was_answered and (
                cdr_info.has_recording or int(cdr_info.billsec or 0) >= 3
            ):
                billsec = cdr_info.billsec or cdr_info.duration or None
                print(
                    f"[kommo_recording] linkedid={linked_id} matched but recording not ready "
                    f"(keeping claim for job retry)",
                    flush=True,
                )
                return PbxCallResolution(None, billsec, cdr_info)

            if claimed and release_linkedid is not None:
                release_linkedid(linked_id)
            print(
                f"[kommo_recording] linkedid={linked_id} unusable — trying next CDR candidate",
                flush=True,
            )

    print(f"[kommo_recording] no CDR match for {phone} ext={extension}", flush=True)
    return PbxCallResolution(None, None, None)


async def resolve_miko_recording(
    *,
    query_cdr: Callable[..., Any],
    download_recording: Callable[[str, Path], Any],
    extension: str,
    phone: str,
    call_time: datetime,
    was_answered: bool,
    call_duration_sec: int,
    answer_time: Optional[datetime] = None,
    work_dir: Optional[Path] = None,
) -> tuple[Optional[str], Optional[int]]:
    """Query CDR with retries and download best matching recording to a temp file."""
    resolution = await resolve_pbx_call(
        query_cdr=query_cdr,
        download_recording=download_recording,
        extension=extension,
        phone=phone,
        call_time=call_time,
        is_incoming=False,
        was_answered=was_answered,
        call_duration_sec=call_duration_sec,
        answer_time=answer_time,
        work_dir=work_dir,
    )
    return resolution.recording_path, resolution.cdr_duration
