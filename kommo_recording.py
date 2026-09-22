"""Resolve Miko PBX recordings for Kommo upload jobs."""

from __future__ import annotations

import logging
import asyncio
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

log = logging.getLogger("kommo_recording")

PBX_CDR_RETRY_DELAYS = [0, 3, 5, 10, 15, 20]
CDR_LOOKUP_DELAYS = [0, 3, 5, 8, 12, 20]
# Defaults; kommo_call_worker.configure() may raise these from config.yaml under load.
MAX_PBX_RECORDING_JOB_RETRIES = 6
JOB_RETRY_WAITS_SEC = [10, 15, 20, 30, 45, 60]
MAX_START_DIFF_SECONDS = 180
# Tight window for recording match — consecutive calls to the same number are often
# seconds apart; a wide window reuses the previous call's CDR/recording.
MAX_RECORDING_START_DIFF_SECONDS = 28
# Ignore CDR legs that clearly ended before this call started (prior call to same number).
CDR_END_BEFORE_CALL_MARGIN_SEC = 1
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
    # Miko finished post-processing every leg of this call (work_completed="1").
    # When True and has_recording is False, the recording will never appear.
    work_completed: bool = False


@dataclass
class PbxCallResolution:
    recording_path: Optional[str]
    cdr_duration: Optional[int]
    cdr_info: Optional[CdrCallInfo]


def configure_pbx_timezone(offset_hours: float) -> None:
    global _pbx_utc_offset_hours
    _pbx_utc_offset_hours = float(offset_hours or 0)


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pbx_local_sql_window(call_time: datetime) -> tuple[str, str]:
    """Build start/end for CDR SQL — Miko stores naive local wall clock."""
    return _pbx_local_sql_window_for_times([call_time])


def _pbx_local_sql_window_for_times(times: list[datetime]) -> tuple[str, str]:
    """Union SQL window covering several UTC call-start anchors."""
    if not times:
        times = [datetime.now(timezone.utc)]
    locals = [
        (_as_utc(t) + timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=None)
        for t in times
    ]
    start = min(locals) - timedelta(hours=2)
    end = max(locals) + timedelta(hours=2)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def build_cdr_match_times(
    call_time: datetime,
    *,
    call_end_time: Optional[datetime] = None,
    answer_time: Optional[datetime] = None,
    call_duration_sec: Optional[int] = None,
    job_created_at: Optional[datetime] = None,
) -> list[datetime]:
    """UTC anchors for matching browser timestamps to PBX-local CDR start.

    Web softphone may enqueue Kommo upload tens of seconds after hangup with a
    ``call_time`` that reflects the delayed upload, not dial start. When
    ``call_time`` is after ``job_created_at``, prefer job-based anchors.
    """
    ct = _as_utc(call_time)
    out: list[datetime] = []
    seen: set[str] = set()

    def add(dt: Optional[datetime]) -> None:
        if dt is None:
            return
        n = _as_utc(dt)
        key = n.isoformat()
        if key in seen:
            return
        seen.add(key)
        out.append(n)

    add(ct)
    dur = int(call_duration_sec or 0)
    cet = _as_utc(call_end_time) if call_end_time else None
    ans = _as_utc(answer_time) if answer_time else None
    jca = _as_utc(job_created_at) if job_created_at else None

    if cet and dur > 0:
        add(cet - timedelta(seconds=dur))
    if ans and dur > 0:
        add(ans - timedelta(seconds=dur))
    if jca:
        add(jca)
        if dur > 0:
            add(jca - timedelta(seconds=dur))
        for pad in (45, 60, 90, 120):
            add(jca - timedelta(seconds=pad))
            add(jca - timedelta(seconds=max(dur, 0) + pad))

    if jca and ct > jca + timedelta(seconds=20):
        preferred = [t for t in out if t <= jca + timedelta(seconds=15)]
        rest = [t for t in out if t not in preferred]
        out = preferred + rest

    return out or [ct]


def _best_call_vs_cdr_diff(rec_time: datetime, cdr_match_times: list[datetime]) -> float:
    if not cdr_match_times:
        return float("inf")
    return min(_call_vs_cdr_diff_seconds(rec_time, t) for t in cdr_match_times)


def is_recording_usable(path: Optional[str]) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _stability_probe_plan(billsec: int) -> tuple[int, float]:
    """How long to wait until a Miko monitor file stops growing (webm mux)."""
    bs = max(0, int(billsec or 0))
    if bs >= 600:
        return 5, 4.0
    if bs >= 180:
        return 4, 3.0
    if bs >= 60:
        return 4, 2.5
    return 3, 2.0


async def wait_recording_file_stable(
    path: Path,
    *,
    billsec: int = 0,
) -> bool:
    """True when file byte size is unchanged across consecutive probes."""
    probes, interval = _stability_probe_plan(billsec)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    for _ in range(probes - 1):
        await asyncio.sleep(interval)
        try:
            new_size = path.stat().st_size
        except OSError:
            return False
        if new_size != size:
            print(
                f"[kommo_recording] recording still growing {path.name}: "
                f"{size} -> {new_size} bytes",
                flush=True,
            )
            return False
        size = new_size
    return True


def _probe_audio_duration_sec(path: Path) -> Optional[float]:
    """Media duration via ffprobe when available (optional sanity check)."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            return None
        return float((result.stdout or "").strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _compressed_min_bytes_per_sec(billsec: int) -> int:
    """Floor bytes/sec for webm/mp3 monitor files (~48–64 kbps Opus)."""
    bs = max(0, int(billsec or 0))
    if bs >= 600:
        return 7000
    if bs >= 120:
        return 6500
    return 5500


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

    compare_duration = call_duration_sec
    if was_answered and answer_time and call_duration_sec > 0:
        pre_answer = (answer_time - call_time).total_seconds()
        if 0 < pre_answer < call_duration_sec:
            compare_duration = max(1, call_duration_sec - int(round(pre_answer)))

    # WebRTC originate: browser timer runs from local auto-answer through PSTN ring
    # to callee/IVR; PBX billsec is talk time on the trunk leg only.
    if cdr_duration_sec and cdr_duration_sec > 0 and compare_duration > cdr_duration_sec:
        if compare_duration >= 15 and cdr_duration_sec <= compare_duration // 2:
            compare_duration = cdr_duration_sec
        elif compare_duration > max(cdr_duration_sec * 4, cdr_duration_sec + 25):
            compare_duration = cdr_duration_sec

    if cdr_duration_sec and cdr_duration_sec > 0 and compare_duration >= 3:
        diff = abs(cdr_duration_sec - compare_duration)
        tolerance = max(30, compare_duration // 2)
        if diff > tolerance:
            return False, (
                f"CDR duration {cdr_duration_sec}s vs talk ~{compare_duration}s "
                f"(diff {diff}s > tolerance {tolerance}s)"
            )

    if not was_answered or compare_duration < 5:
        return True, None

    pbx_file = Path(pbx_path)
    try:
        pbx_bytes = pbx_file.stat().st_size
    except OSError:
        return False, "PBX file stat failed"

    duration_for_size = (
        cdr_duration_sec if cdr_duration_sec and cdr_duration_sec > 0 else compare_duration
    )
    compressed = str(pbx_path).lower().endswith((".webm", ".mp3", ".ogg"))

    if compressed and duration_for_size >= 30:
        min_bps = _compressed_min_bytes_per_sec(duration_for_size)
        min_bytes = max(8000, duration_for_size * min_bps)
        if pbx_bytes < min_bytes:
            return False, (
                f"PBX file too small ({pbx_bytes} bytes < min {min_bytes} "
                f"for ~{duration_for_size}s talk)"
            )
    elif not compressed:
        floor_bytes = 8000 if duration_for_size < 30 else 50_000
        min_bps = 1500 if duration_for_size < 30 else 4000
        min_bytes = max(floor_bytes, duration_for_size * min_bps)
        if pbx_bytes < min_bytes:
            return False, (
                f"PBX file too small ({pbx_bytes} bytes < min {min_bytes} "
                f"for ~{duration_for_size}s)"
            )

    if duration_for_size >= 45 and compressed:
        probed = _probe_audio_duration_sec(pbx_file)
        if probed is not None:
            # Reject truncated mux (e.g. 105s file uploaded for 902s CDR billsec).
            min_ratio = 0.82 if duration_for_size >= 120 else 0.75
            if probed + 3 < duration_for_size * min_ratio:
                return False, (
                    f"probed audio {probed:.1f}s vs CDR talk {duration_for_size}s "
                    f"(file likely still muxing or incomplete)"
                )

    return True, None


_PBX_RECORDING_SUFFIXES = (".webm", ".mp3", ".wav", ".ogg")


def _recording_suffixes_for_linkedid(records: list[dict], linkedid: str) -> tuple[str, ...]:
    """Use the Miko recording extension from CDR — do not fall back to .mp3 for .webm calls.

    Trying .mp3 after a failed .webm copy still reads the same growing file from disk
    and saves it under a wrong name (Kommo then transcodes truncated audio).
    """
    for r in records:
        if (r.get("linkedid") or r.get("linked_id")) != linkedid:
            continue
        rec = str(r.get("recording") or r.get("recordingfile") or "").strip()
        if rec:
            suffix = Path(rec).suffix.lower()
            if suffix in _PBX_RECORDING_SUFFIXES:
                return (suffix,)
    return _PBX_RECORDING_SUFFIXES


async def _download_linkedid_recording(
    *,
    download_recording: Callable[[str, Path], Any],
    linkedid: str,
    work_dir: Path,
    records: list[dict],
    cdr_info: CdrCallInfo,
    was_answered: bool,
    call_duration_sec: int,
    answer_time: Optional[datetime],
    call_time: datetime,
) -> Optional[str]:
    """Try direct download by linkedid; Miko often has the file before CDR lists recordingfile."""
    if pbx_recording_definitely_absent(cdr_info):
        print(
            f"[kommo_recording] linkedid={linkedid} work_completed=1 without recordingfile "
            f"— skip download",
            flush=True,
        )
        return None
    # CDR billsec is final at hangup, but Miko may still be muxing wav48 → webm.
    # Downloading too early yields a truncated file while the Kommo note uses full billsec.
    if (
        cdr_info.was_answered
        and cdr_info.billsec > 0
        and not cdr_info.work_completed
    ):
        print(
            f"[kommo_recording] linkedid={linkedid} work_completed=0 billsec={cdr_info.billsec} "
            f"— wait for Miko recording post-processing",
            flush=True,
        )
        return None
    billsec_hint = int(cdr_info.billsec or cdr_info.duration or 0)
    if billsec_hint >= 600:
        await asyncio.sleep(12)
    elif billsec_hint >= 180:
        await asyncio.sleep(6)
    elif billsec_hint >= 60:
        await asyncio.sleep(3)
    cdr_duration = cdr_info.billsec or cdr_info.duration or None
    suffixes = _recording_suffixes_for_linkedid(records, linkedid)
    for rec_attempt, rec_delay in enumerate(PBX_CDR_RETRY_DELAYS):
        if rec_delay > 0:
            await asyncio.sleep(rec_delay)
            if rec_attempt > 0:
                msg = (
                    f"[kommo_recording] recording download retry {rec_attempt} "
                    f"linkedid={linkedid}"
                )
                log.info(msg)
                print(msg, flush=True)
        for suffix in suffixes:
            dest = work_dir / f"mikopbx_{linkedid}{suffix}"
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                try:
                    ok = await download_recording(
                        linkedid, dest, billsec=billsec_hint
                    )
                except TypeError:
                    ok = await download_recording(linkedid, dest)
            except Exception as exc:
                log.warning("recording download failed: %s", exc)
                print(
                    f"[kommo_recording] download failed linkedid={linkedid}: {exc}",
                    flush=True,
                )
                continue
            if not ok or not dest.is_file():
                print(
                    f"[kommo_recording] download empty linkedid={linkedid} suffix={suffix}",
                    flush=True,
                )
                continue
            if not await wait_recording_file_stable(dest, billsec=billsec_hint):
                print(
                    f"[kommo_recording] linkedid={linkedid} downloaded file still growing "
                    f"— wait for Miko mux",
                    flush=True,
                )
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            acceptable, reason = is_pbx_recording_acceptable(
                was_answered=was_answered,
                call_duration_sec=call_duration_sec,
                answer_time=answer_time,
                call_time=call_time,
                pbx_path=str(dest),
                cdr_duration_sec=cdr_duration,
            )
            if acceptable:
                print(
                    f"[kommo_recording] recording ready linkedid={linkedid} "
                    f"path={dest.name} bytes={dest.stat().st_size}",
                    flush=True,
                )
                return str(dest)
            log.info("PBX recording rejected: %s", reason)
            print(
                f"[kommo_recording] rejected linkedid={linkedid} ({dest.name}): {reason}",
                flush=True,
            )
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
    return None


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


def _cdr_start_as_utc(rec_time: datetime) -> datetime:
    """Convert naive PBX-local CDR start to UTC for comparisons with client call_time."""
    if rec_time.tzinfo is not None:
        return rec_time.astimezone(timezone.utc)
    if _pbx_utc_offset_hours:
        return (rec_time - timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=timezone.utc)
    return rec_time.replace(tzinfo=timezone.utc)


def _cdr_leg_ended_before_call(
    rec_time: datetime,
    billsec: int,
    cdr_match_times: list[datetime],
    *,
    margin_sec: int = CDR_END_BEFORE_CALL_MARGIN_SEC,
) -> bool:
    """True when a finished CDR leg ended before this call could have started."""
    if billsec <= 0 or not cdr_match_times:
        return False
    leg_start = _cdr_start_as_utc(rec_time)
    leg_end = leg_start + timedelta(seconds=billsec)
    earliest_start = min(cdr_match_times, key=_as_utc)
    ct = _as_utc(earliest_start)
    if leg_end > ct - timedelta(seconds=max(0, margin_sec)):
        return False
    best_start_diff = _best_call_vs_cdr_diff(rec_time, cdr_match_times)
    if best_start_diff <= MAX_RECORDING_START_DIFF_SECONDS:
        return False
    return True


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


def _linkedids_matching_phone(
    records: list[dict],
    *,
    phone: str,
    is_incoming: bool,
) -> set[str]:
    """linkedids where at least one leg matches the client destination number."""
    out: set[str] = set()
    for r in records:
        linkedid = str(r.get("linkedid") or r.get("linked_id") or "")
        if not linkedid:
            continue
        if _row_matches_phone(r, phone, is_incoming=is_incoming):
            out.add(linkedid)
    return out


def _outbound_trunk_peer_leg(
    record: dict,
    *,
    phone_linkedids: set[str],
) -> bool:
    """Trunk leg on same linkedid as the dial leg — recording often lives here.

    Miko may store outbound CallerID in dst_num on the trunk row (not the
    callee), so strict phone match would skip the only row with recordingfile.
    """
    linkedid = str(record.get("linkedid") or record.get("linked_id") or "")
    return bool(
        linkedid
        and linkedid in phone_linkedids
        and _is_trunk_leg(record)
    )


def _int_field(record: dict, *keys: str) -> int:
    for key in keys:
        try:
            return int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _linkedid_billsec_map(
    records: list[dict],
    *,
    phone: str,
    is_incoming: bool,
) -> dict[str, int]:
    """Max billsec per linkedid across phone-matching legs (incl. outbound trunk peers)."""
    phone_linkedids = (
        _linkedids_matching_phone(records, phone=phone, is_incoming=is_incoming)
        if not is_incoming
        else set()
    )
    out: dict[str, int] = {}
    for r in records:
        linkedid = str(r.get("linkedid") or r.get("linked_id") or "")
        if not linkedid:
            continue
        phone_match = _row_matches_phone(r, phone, is_incoming=is_incoming)
        trunk_peer = _outbound_trunk_peer_leg(r, phone_linkedids=phone_linkedids)
        if not phone_match and not trunk_peer:
            continue
        bs = _int_field(r, "billsec", "duration")
        out[linkedid] = max(out.get(linkedid, 0), bs)
    return out


def pbx_recording_definitely_absent(cdr_info: CdrCallInfo) -> bool:
    """True when Miko finished post-processing and no recording will ever appear."""
    if not cdr_info.work_completed or cdr_info.has_recording:
        return False
    # Answered calls with talk time may still have a file on disk even when the
    # CDR recording field is empty (WebRTC/trunk leg timing).
    if cdr_info.was_answered and cdr_info.billsec > 0:
        return False
    return True


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
    cdr_match_times: Optional[list[datetime]] = None,
) -> list[dict]:
    """Rank CDR rows for Kommo sync (best time match first, skip already-used linkedids)."""
    if call_time.tzinfo is None:
        call_time = call_time.replace(tzinfo=timezone.utc)
    match_times = cdr_match_times or [call_time]

    excluded = exclude_linkedids or set()
    max_start_diff = (
        MAX_RECORDING_START_DIFF_SECONDS if require_recording_match else MAX_START_DIFF_SECONDS
    )
    phone_linkedids = (
        _linkedids_matching_phone(records, phone=phone, is_incoming=is_incoming)
        if not is_incoming
        else set()
    )
    linkedid_billsec = _linkedid_billsec_map(
        records, phone=phone, is_incoming=is_incoming
    )
    scored: list[tuple[float, dict]] = []

    for r in records:
        linkedid = str(r.get("linkedid") or r.get("linked_id") or "")
        if not linkedid or linkedid in excluded:
            continue
        rec_time = _parse_cdr_start(r.get("start") or r.get("calldate"))
        if not rec_time:
            continue
        phone_match = _row_matches_phone(r, phone, is_incoming=is_incoming)
        trunk_peer = _outbound_trunk_peer_leg(r, phone_linkedids=phone_linkedids)
        if not phone_match and not trunk_peer:
            continue
        diff = _best_call_vs_cdr_diff(rec_time, match_times)
        if require_recording_match:
            row_max_diff = max_start_diff
        elif trunk_peer:
            row_max_diff = MAX_START_DIFF_SECONDS
        else:
            row_max_diff = max_start_diff
        if diff > row_max_diff:
            continue

        billsec = _int_field(r, "billsec", "duration")
        agg_billsec = linkedid_billsec.get(linkedid, billsec)
        effective_billsec = agg_billsec if (trunk_peer or billsec <= 0) else billsec
        if _cdr_leg_ended_before_call(rec_time, effective_billsec, match_times):
            continue
        dur_billsec = agg_billsec if agg_billsec > 0 else billsec
        if (
            require_recording_match
            and call_duration_sec
            and call_duration_sec > 0
            and dur_billsec > 0
        ):
            dur_diff = abs(dur_billsec - call_duration_sec)
            tolerance = max(12, min(30, call_duration_sec // 3))
            long_ring_to_callee = (
                dur_billsec < call_duration_sec // 2 and call_duration_sec >= 15
            )
            client_under_reported = (
                dur_billsec > call_duration_sec * 1.5 and call_duration_sec >= 10
            )
            if (
                not long_ring_to_callee
                and not client_under_reported
                and dur_diff > tolerance
                and diff > 25
            ):
                continue

        score = diff
        if require_recording_match:
            rec_start_utc = _cdr_start_as_utc(rec_time)
            earliest_call = min(match_times, key=_as_utc)
            if rec_start_utc < _as_utc(earliest_call) - timedelta(seconds=8):
                score += 25
            if trunk_peer and not phone_match:
                score += 20
        elif trunk_peer or (not is_incoming and _is_trunk_leg(r)):
            score -= 60
        score_billsec = dur_billsec if dur_billsec > 0 else billsec
        if score_billsec > 0:
            score -= min(score_billsec, 30)
        if call_duration_sec and call_duration_sec > 0 and score_billsec > 0:
            dur_diff = abs(score_billsec - call_duration_sec)
            if (
                require_recording_match
                and score_billsec > call_duration_sec * 1.5
                and call_duration_sec >= 10
            ):
                score += min(dur_diff * 0.2, 12)
            else:
                score += dur_diff * 0.75
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
    if not is_incoming and phone_rows:
        phone_linkedids = {linkedid}
        trunk_peer_rows = [
            r for r in rows if _outbound_trunk_peer_leg(r, phone_linkedids=phone_linkedids)
        ]
    else:
        trunk_peer_rows = []
    trunk_rows = [r for r in phone_rows if _is_trunk_leg(r)] if not is_incoming else []
    trunk_rows = trunk_rows or trunk_peer_rows
    primary_candidates = trunk_rows or phone_rows or rows
    primary = primary_candidates[0] if primary_candidates else {}

    has_recording = any(str(r.get("recording") or "").strip() for r in rows)
    work_completed = bool(rows) and all(
        str(r.get("work_completed") or "").strip() == "1" for r in rows
    )
    agg_rows = rows if not is_incoming else (phone_rows or rows)
    billsec = max((_int_field(r, "billsec") for r in agg_rows), default=0)
    duration = max((_int_field(r, "duration") for r in agg_rows), default=0)

    disposition = (primary.get("disposition") or "").upper()
    for r in agg_rows:
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
        work_completed=work_completed,
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
    dst: Optional[str] = None,
    linkedid: Optional[str] = None,
    is_incoming: bool = False,
    sql_window: Optional[tuple[str, str]] = None,
) -> list[dict]:
    kwargs: dict[str, Any] = {"limit": 200}
    if linkedid:
        kwargs["linkedid"] = linkedid
    elif not wide:
        kwargs["ext"] = extension
    if dst and not linkedid:
        kwargs["dst"] = dst
        kwargs["is_incoming"] = is_incoming
    if call_time is not None and not linkedid:
        kwargs["start_from"], kwargs["start_to"] = sql_window or _pbx_local_sql_window(call_time)
    label = "linkedid CDR" if linkedid else ("wide CDR" if wide else "CDR")
    if dst and not linkedid:
        label = f"phone CDR dst={dst}"
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


def _merge_cdr_record_batches(batches: list[list[dict]]) -> list[dict]:
    combined: list[dict] = []
    seen_row: set[str] = set()
    for batch in batches:
        for row in batch:
            linkedid = str(row.get("linkedid") or row.get("linked_id") or "")
            start = str(row.get("start") or row.get("calldate") or "")
            src = str(row.get("src_num") or row.get("src") or "")
            dst = str(row.get("dst_num") or row.get("dst") or "")
            key = f"{linkedid}|{start}|{src}|{dst}"
            if key in seen_row:
                continue
            seen_row.add(key)
            combined.append(row)
    return combined


async def _enrich_records_for_linkedid(
    query_cdr: Callable[..., Any],
    linkedid: str,
    records: list[dict],
) -> list[dict]:
    """Load every CDR leg for *linkedid* (trunk row with recording is often missing)."""
    lid = (linkedid or "").strip()
    if not lid:
        return records
    extra = await _query_cdr_records(
        query_cdr,
        "",
        call_time=None,
        linkedid=lid,
        wide=True,
    )
    if not extra:
        return records
    return _merge_cdr_record_batches([records, extra])


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
    answer_time: Optional[datetime] = None,
    job_created_at: Optional[datetime] = None,
    log_attempt: bool = False,
    require_recording_match: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Merge narrow + wide CDR queries, return verified candidates best-first."""

    match_times = build_cdr_match_times(
        call_time,
        call_end_time=call_end_time,
        answer_time=answer_time,
        call_duration_sec=call_duration_sec,
        job_created_at=job_created_at,
    )
    sql_window = _pbx_local_sql_window_for_times(match_times)
    query_anchor = match_times[0]

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
    phone_rows = 0
    wide_rows = 0
    narrow_rows = 0
    if not is_incoming and phone:
        phone_records = await _query_cdr_records(
            query_cdr,
            extension,
            query_anchor,
            dst=phone,
            is_incoming=False,
            sql_window=sql_window,
        )
        phone_rows = len(phone_records)
        if phone_records:
            record_batches.append(phone_records)
    if not is_incoming:
        wide_records = await _query_cdr_records(
            query_cdr, extension, query_anchor, wide=True, sql_window=sql_window
        )
        wide_rows = len(wide_records)
        if wide_records:
            record_batches.append(wide_records)
    narrow_records = await _query_cdr_records(
        query_cdr, extension, query_anchor, sql_window=sql_window
    )
    narrow_rows = len(narrow_records)
    if narrow_records:
        record_batches.append(narrow_records)

    combined = _merge_cdr_record_batches(record_batches)

    if log_attempt:
        print(
            f"[kommo_recording] CDR candidates ext={extension} phone={phone} "
            f"window={sql_window[0]}..{sql_window[1]} "
            f"rows_phone={phone_rows} rows_wide={wide_rows} rows_ext={narrow_rows} "
            f"rows_combined={len(combined)} "
            f"excluded_linkedids={len(exclude_linkedids or ())} "
            f"match_times={len(match_times)}",
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
        cdr_match_times=match_times,
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

    if log_attempt and ranked and not verified:
        print(
            f"[kommo_recording] ranked={len(ranked)} but verified=0 for phone={phone} "
            f"ext={extension} (linkedid ownership check failed)",
            flush=True,
        )
    elif log_attempt and not ranked:
        phone_lids = _linkedids_matching_phone(combined, phone=phone, is_incoming=is_incoming)
        excluded_set = exclude_linkedids or set()
        blocked = phone_lids & excluded_set if phone_lids else set()
        if blocked:
            print(
                f"[kommo_recording] no ranked CDR for phone={phone}: "
                f"matching linkedid(s) excluded={sorted(blocked)}",
                flush=True,
            )
        else:
            best_diff = float("inf")
            for row in combined:
                if not _row_matches_phone(row, phone, is_incoming=is_incoming):
                    continue
                rec_time = _parse_cdr_start(row.get("start") or row.get("calldate"))
                if not rec_time:
                    continue
                best_diff = min(best_diff, _best_call_vs_cdr_diff(rec_time, match_times))
            skew = ""
            if job_created_at and _as_utc(call_time) > _as_utc(job_created_at) + timedelta(seconds=20):
                skew = f" call_time_skew={(_as_utc(call_time) - _as_utc(job_created_at)).total_seconds():.0f}s"
            print(
                f"[kommo_recording] no ranked CDR for phone={phone} ext={extension}"
                f"{skew}"
                + (
                    f" best_start_diff={best_diff:.0f}s"
                    if best_diff != float("inf")
                    else ""
                ),
                flush=True,
            )

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
    call_end_time: Optional[datetime] = None,
    answer_time: Optional[datetime] = None,
    job_created_at: Optional[datetime] = None,
) -> tuple[Optional[dict], list[dict]]:
    """Find best CDR row for Kommo note; wide query fallback for originate trunk legs."""

    match_times = build_cdr_match_times(
        call_time,
        call_end_time=call_end_time,
        answer_time=answer_time,
        call_duration_sec=call_duration_sec,
        job_created_at=job_created_at,
    )
    sql_window = _pbx_local_sql_window_for_times(match_times)
    query_anchor = match_times[0]

    async def _pick_from_records(records: list[dict]) -> Optional[dict]:
        ranked = rank_cdr_records_for_note(
            records,
            phone=phone,
            call_time=call_time,
            is_incoming=is_incoming,
            exclude_linkedids=exclude_linkedids,
            call_duration_sec=call_duration_sec,
            cdr_match_times=match_times,
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
        wide_records = await _query_cdr_records(
            query_cdr, extension, query_anchor, wide=True, sql_window=sql_window
        )
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

    # Originate/outbound: dial leg may only appear under dst_num, not ext filter.
    if not is_incoming and phone:
        phone_records = await _query_cdr_records(
            query_cdr,
            extension,
            query_anchor,
            dst=phone,
            is_incoming=False,
            sql_window=sql_window,
        )
        if phone_records:
            phone_best = await _pick_from_records(phone_records)
            if phone_best:
                if log_attempt:
                    linked_id = str(
                        phone_best.get("linkedid") or phone_best.get("linked_id") or ""
                    )
                    print(
                        f"[kommo_recording] phone CDR matched linkedid={linked_id} "
                        f"rows={len(phone_records)}",
                        flush=True,
                    )
                return phone_best, phone_records

    # Originate/outbound: recording is usually on the trunk leg (outside ext filter).
    if not is_incoming:
        wide_best, wide_records = await _try_wide()
        if wide_best:
            return wide_best, wide_records

    all_records = await _query_cdr_records(
        query_cdr, extension, query_anchor, sql_window=sql_window
    )
    if log_attempt:
        print(
            f"[kommo_recording] CDR query ext={extension} phone={phone} "
            f"window={sql_window[0]}..{sql_window[1]} rows={len(all_records)} "
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


async def _try_resolve_known_linkedid(
    *,
    known_linkedid: str,
    query_cdr: Callable[..., Any],
    download_recording: Callable[[str, Path], Any],
    phone: str,
    is_incoming: bool,
    was_answered: bool,
    call_duration_sec: int,
    call_from_label: Optional[str],
    answer_time: Optional[datetime],
    call_time: datetime,
    work_dir: Path,
    claim_linkedid: Optional[Callable[[str], bool]] = None,
    release_linkedid: Optional[Callable[[str], None]] = None,
) -> Optional[PbxCallResolution]:
    """Fast path for Kommo worker retries — CDR linkedid already claimed."""
    linked_id = (known_linkedid or "").strip()
    if not linked_id:
        return None
    claimed = True
    if claim_linkedid is not None:
        claimed = bool(claim_linkedid(linked_id))
        if not claimed:
            print(
                f"[kommo_recording] known linkedid={linked_id} — claim held by another job",
                flush=True,
            )
            return None
    enriched = await _enrich_records_for_linkedid(query_cdr, linked_id, [])
    cdr_info = summarize_cdr_linkedid(
        enriched,
        linked_id,
        phone=phone,
        is_incoming=is_incoming,
        call_from_label=call_from_label,
    )
    print(
        f"[kommo_recording] known linkedid={linked_id} "
        f"disposition={cdr_info.disposition} billsec={cdr_info.billsec} "
        f"has_recording={cdr_info.has_recording} work_completed={cdr_info.work_completed}",
        flush=True,
    )
    if pbx_recording_definitely_absent(cdr_info):
        return PbxCallResolution(
            None,
            cdr_info.billsec or cdr_info.duration or None,
            cdr_info,
        )
    recording_path = await _download_linkedid_recording(
        download_recording=download_recording,
        linkedid=linked_id,
        work_dir=work_dir,
        records=enriched,
        cdr_info=cdr_info,
        was_answered=was_answered,
        call_duration_sec=call_duration_sec,
        answer_time=answer_time,
        call_time=call_time,
    )
    if recording_path:
        return PbxCallResolution(
            recording_path,
            cdr_info.billsec or cdr_info.duration or None,
            cdr_info,
        )
    return PbxCallResolution(
        None,
        cdr_info.billsec or cdr_info.duration or None,
        cdr_info,
    )


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
    job_created_at: Optional[datetime] = None,
    work_dir: Optional[Path] = None,
    verify_linkedid: Optional[Callable[..., Union[bool, Awaitable[bool]]]] = None,
    exclude_linkedids: Optional[set[str]] = None,
    claim_linkedid: Optional[Callable[[str], bool]] = None,
    release_linkedid: Optional[Callable[[str], None]] = None,
    known_linkedid: Optional[str] = None,
    quick_cdr_pass: bool = False,
) -> PbxCallResolution:
    """Resolve PBX CDR for Kommo: recording file and/or call note metadata."""

    work_dir = work_dir or Path(tempfile.gettempdir()) / "callspire_kommo_recordings"
    work_dir.mkdir(parents=True, exist_ok=True)

    if known_linkedid:
        fast = await _try_resolve_known_linkedid(
            known_linkedid=known_linkedid,
            query_cdr=query_cdr,
            download_recording=download_recording,
            phone=phone,
            is_incoming=is_incoming,
            was_answered=was_answered,
            call_duration_sec=call_duration_sec,
            call_from_label=call_from_label,
            answer_time=answer_time,
            call_time=call_time,
            work_dir=work_dir,
            claim_linkedid=claim_linkedid,
            release_linkedid=release_linkedid,
        )
        if fast is not None:
            return fast

    lookup_delays = [0] if quick_cdr_pass else CDR_LOOKUP_DELAYS

    for attempt, delay in enumerate(lookup_delays):
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
            answer_time=answer_time,
            job_created_at=job_created_at,
            log_attempt=(attempt == 0),
            require_recording_match=True,
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

            enriched_records = await _enrich_records_for_linkedid(
                query_cdr, linked_id, all_records
            )
            cdr_info = summarize_cdr_linkedid(
                enriched_records,
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

            if pbx_recording_definitely_absent(cdr_info):
                return PbxCallResolution(
                    None,
                    cdr_info.billsec or cdr_info.duration or None,
                    cdr_info,
                )

            if not cdr_info.has_recording:
                print(
                    f"[kommo_recording] linkedid={linked_id} recording flag not set in CDR batch "
                    f"— trying direct download by linkedid",
                    flush=True,
                )

            recording_path = await _download_linkedid_recording(
                download_recording=download_recording,
                linkedid=linked_id,
                work_dir=work_dir,
                records=enriched_records,
                cdr_info=cdr_info,
                was_answered=was_answered,
                call_duration_sec=call_duration_sec,
                answer_time=answer_time,
                call_time=call_time,
            )
            if recording_path:
                cdr_duration = cdr_info.billsec or cdr_info.duration or None
                return PbxCallResolution(recording_path, cdr_duration, cdr_info)

            last_lookup_attempt = attempt >= len(lookup_delays) - 1
            if last_lookup_attempt:
                print(
                    f"[kommo_recording] linkedid={linked_id} recording unavailable "
                    f"— Kommo call note only",
                    flush=True,
                )
                return PbxCallResolution(
                    None,
                    cdr_info.billsec or cdr_info.duration or None,
                    cdr_info,
                )
            if claimed and release_linkedid is not None:
                release_linkedid(linked_id)
            print(
                f"[kommo_recording] linkedid={linked_id} unusable — trying next CDR candidate",
                flush=True,
            )

    note_row, note_records = await _pick_cdr_for_note(
        query_cdr=query_cdr,
        verify_linkedid=verify_linkedid,
        extension=extension,
        phone=phone,
        call_time=call_time,
        is_incoming=is_incoming,
        log_attempt=True,
        exclude_linkedids=exclude_linkedids,
        call_duration_sec=call_duration_sec or None,
        call_end_time=call_end_time,
        answer_time=answer_time,
        job_created_at=job_created_at,
    )
    if note_row:
        linked_id = str(note_row.get("linkedid") or note_row.get("linked_id") or "")
        if linked_id:
            claimed = True
            if claim_linkedid is not None:
                claimed = bool(claim_linkedid(linked_id))
            if claimed:
                note_enriched = await _enrich_records_for_linkedid(
                    query_cdr, linked_id, note_records
                )
                cdr_info = summarize_cdr_linkedid(
                    note_enriched,
                    linked_id,
                    phone=phone,
                    is_incoming=is_incoming,
                    call_from_label=call_from_label,
                )
                print(
                    f"[kommo_recording] CDR note fallback linkedid={linked_id} "
                    f"disposition={cdr_info.disposition} result={cdr_info.kommo_call_result}",
                    flush=True,
                )
                recording_path = await _download_linkedid_recording(
                    download_recording=download_recording,
                    linkedid=linked_id,
                    work_dir=work_dir,
                    records=note_enriched,
                    cdr_info=cdr_info,
                    was_answered=was_answered,
                    call_duration_sec=call_duration_sec,
                    answer_time=answer_time,
                    call_time=call_time,
                )
                if recording_path:
                    cdr_duration = cdr_info.billsec or cdr_info.duration or None
                    return PbxCallResolution(recording_path, cdr_duration, cdr_info)
                return PbxCallResolution(
                    None,
                    cdr_info.billsec or cdr_info.duration or None,
                    cdr_info,
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
