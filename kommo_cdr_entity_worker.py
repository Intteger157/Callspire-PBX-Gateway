"""Apply Kommo entity rules from Miko CDR for unanswered inbound calls.

Runs independently of softphone ``process-call`` jobs — when an external caller
reaches the PBX (queue, IVR, ring timeout) without a client upload, we still
create contacts/leads/tasks per admin entity rules.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import kommo_jobs_db
import permissions_db
from cdr_client import aggregate_cdr_calls, query_cdr
from kommo_crm import KommoCrmClient
from kommo_entity_rules import apply_entity_rule, list_rules

log = logging.getLogger("kommo_cdr_entity_worker")

POLL_SECONDS = 30
MIN_CALL_AGE_SECONDS = 60
LOOKBACK_MINUTES = 180
CDR_SCAN_LIMIT = 5000

_running = False
_task: Optional[asyncio.Task] = None
_cfg: dict[str, Any] = {}
_get_session: Optional[Callable[[str], Awaitable[Optional[dict[str, Any]]]]] = None
_query_cdr_fn: Optional[Callable[..., Awaitable[list]]] = None
_worker_started_at_utc: Optional[datetime] = None
_no_rule_linkedids: set[str] = set()


def _is_cdr_entity_processed(linkedid: str) -> bool:
    fn = getattr(kommo_jobs_db, "is_cdr_entity_processed", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(linkedid))
    except Exception:
        return False


def _cdr_call_has_active_kommo_job(**kwargs: Any) -> bool:
    fn = getattr(kommo_jobs_db, "cdr_call_has_active_kommo_job", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(**kwargs))
    except Exception:
        return False


def configure(cfg: dict[str, Any] | None = None) -> None:
    global _cfg, POLL_SECONDS, MIN_CALL_AGE_SECONDS, LOOKBACK_MINUTES
    if not cfg:
        return
    _cfg = cfg
    POLL_SECONDS = max(
        10, min(300, int(cfg.get("kommo_cdr_entity_poll_seconds") or POLL_SECONDS))
    )
    MIN_CALL_AGE_SECONDS = max(
        15,
        min(600, int(cfg.get("kommo_cdr_entity_min_call_age_seconds") or MIN_CALL_AGE_SECONDS)),
    )
    LOOKBACK_MINUTES = max(
        30, min(1440, int(cfg.get("kommo_cdr_entity_lookback_minutes") or LOOKBACK_MINUTES))
    )


def _enabled() -> bool:
    if not _cfg.get("kommo_cdr_entity_enabled", True):
        return False
    try:
        cfg_row = permissions_db.get_kommo_integration()
        if not cfg_row.get("enabled"):
            return False
        tokens = permissions_db.get_kommo_oauth_tokens()
        return bool((tokens.get("access_token") or "").strip())
    except Exception:
        return False


def _pbx_offset_hours() -> float:
    try:
        return float(_cfg.get("pbx_utc_offset_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _pbx_local_window(minutes: int) -> tuple[str, str]:
    offset = _pbx_offset_hours()
    now_utc = datetime.now(timezone.utc)
    end_local = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
    start_local = end_local - timedelta(minutes=max(30, minutes))
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_local.strftime(fmt), end_local.strftime(fmt)


def _parse_pbx_local_start(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.strptime(text[:19], fmt)
            offset = _pbx_offset_hours()
            return (local.replace(tzinfo=timezone.utc) - timedelta(hours=offset))
        except ValueError:
            continue
    return None


def _call_start_utc(call: dict[str, Any]) -> Optional[datetime]:
    return _parse_pbx_local_start(call.get("start") or "")


def _call_is_after_worker_start(call: dict[str, Any]) -> bool:
    if not _worker_started_at_utc:
        return True
    call_start = _call_start_utc(call)
    return call_start is not None and call_start >= _worker_started_at_utc


def _known_extensions() -> set[str]:
    exts: set[str] = set()
    try:
        exts.update(permissions_db.list_kommo_extension_users().keys())
    except Exception:
        pass
    config_db = (_cfg.get("config_db_path") or "").strip()
    if config_db and Path(config_db).is_file():
        try:
            conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT extension FROM m_Sip WHERE type='peer' AND disabled='0'"
                ).fetchall()
                for row in rows:
                    ext = str(row[0] or "").strip()
                    if ext:
                        exts.add(ext)
            finally:
                conn.close()
        except Exception:
            pass
    return exts


def _normalize_phone(value: str) -> str:
    digits = "".join(c for c in (value or "") if c.isdigit())
    if not digits:
        return (value or "").strip()
    if len(digits) >= 10 and not (value or "").strip().startswith("+"):
        return f"+{digits}"
    return (value or "").strip()


def _digit_len(value: str) -> int:
    return len("".join(c for c in str(value or "") if c.isdigit()))


def _is_internal_num(num: str, known_exts: set[str]) -> bool:
    n = (num or "").strip()
    if not n:
        return False
    if n in known_exts or n.removesuffix("-WS") in known_exts:
        return True
    # Fallback when extension list is unavailable: short numeric ids are internal.
    return not known_exts and n.isdigit() and len(n) <= 5


def _extension_answered(legs: list[dict], known_exts: set[str]) -> bool:
    """True when an internal extension leg was answered (ignore trunk/IVR ANSWERED)."""
    for leg in legs:
        dst = (leg.get("dst_num") or "").strip()
        if (
            (leg.get("disposition") or "").upper() == "ANSWERED"
            and _is_internal_num(dst, known_exts)
        ):
            return True
    return False


def _leg_trunk_marker(leg: dict) -> str:
    """Miko stores inbound trunk on ``from_account``, outbound on ``to_account``."""
    for key in ("trunk", "to_account", "from_account"):
        val = (leg.get(key) or "").strip()
        if val:
            return val
    return ""


def _leg_has_inbound_trunk_channel(leg: dict) -> bool:
    for chan in (leg.get("src_chan") or "", leg.get("dst_chan") or ""):
        upper = chan.upper()
        if "SIP-TRUNK" in upper or "TRUNK-" in upper:
            return True
    return False


def _call_has_inbound_trunk_leg(legs: list[dict], known_exts: set[str]) -> bool:
    for leg in legs:
        from_acc = (leg.get("from_account") or "").strip()
        to_acc = (leg.get("to_account") or "").strip()
        if from_acc and not to_acc:
            return True
    for leg in legs:
        if not _leg_has_inbound_trunk_channel(leg):
            continue
        src = (leg.get("src_num") or "").strip()
        if _digit_len(src) >= 6 and not _is_internal_num(src, known_exts):
            return True
    return False


def _call_involves_outbound_extension(legs: list[dict], known_exts: set[str]) -> bool:
    for leg in legs:
        src = (leg.get("src_num") or "").strip()
        if _is_internal_num(src, known_exts):
            return True
        if (leg.get("to_account") or "").strip() and not (leg.get("from_account") or "").strip():
            # Outbound trunk leg: to_account set, caller is not the remote party on src.
            dst = (leg.get("dst_num") or "").strip()
            if _digit_len(dst) >= 6 and not _is_internal_num(dst, known_exts):
                return True
        for chan in (leg.get("src_chan") or "", leg.get("dst_chan") or ""):
            for ext in known_exts:
                base = ext.removesuffix("-WS")
                if f"/{base}-" in chan or f"/{ext}-" in chan:
                    return True
    return False


def _leg_is_minimal_miko_inbound(leg: dict, known_exts: set[str]) -> bool:
    """Miko often writes DID/IVR calls as caller-only rows (no dst/account fields)."""
    src = (leg.get("src_num") or "").strip()
    dst = (leg.get("dst_num") or "").strip()
    if _digit_len(src) < 6 or _is_internal_num(src, known_exts):
        return False
    if dst or (leg.get("from_account") or "").strip() or (leg.get("to_account") or "").strip():
        return False
    disp = (leg.get("disposition") or "").upper()
    return disp in ("NOANSWER", "BUSY", "FAILED", "CONGESTION", "ANSWERED")


def _extract_missed_inbound_calls(
    rows: list[dict[str, Any]], known_exts: set[str]
) -> list[dict[str, Any]]:
    """Unanswered inbound calls from raw CDR legs (DID/IVR/queue without extension answer)."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for i, row in enumerate(rows):
        key = (row.get("linkedid") or "").strip() or f"__row{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    missed: list[dict[str, Any]] = []
    for key in order:
        if key.startswith("__row"):
            continue
        legs = groups[key]
        real = [
            leg
            for leg in legs
            if (leg.get("appname") or "").strip().lower() != "originate"
        ] or legs

        if _extension_answered(real, known_exts):
            continue

        if _call_involves_outbound_extension(real, known_exts):
            continue

        ext = ""
        phone = ""
        did = ""

        # Extension rang (including queue) but nobody answered.
        for leg in real:
            src = (leg.get("src_num") or "").strip()
            dst = (leg.get("dst_num") or "").strip()
            if (
                _is_internal_num(dst, known_exts)
                and _digit_len(src) >= 6
                and not _is_internal_num(src, known_exts)
            ):
                phone = src
                ext = dst.removesuffix("-WS")
                break

        # DID / IVR on trunk — caller on src_num, DID often in dst_num.
        if not phone and _call_has_inbound_trunk_leg(real, known_exts):
            for leg in real:
                src = (leg.get("src_num") or "").strip()
                dst = (leg.get("dst_num") or "").strip()
                if _digit_len(src) >= 6 and not _is_internal_num(src, known_exts):
                    phone = src
                    if _digit_len(dst) >= 6 and not _is_internal_num(dst, known_exts):
                        did = dst
                    break

        # Miko minimal row: src_num=caller only, empty dst/from_account/to_account.
        if not phone:
            for leg in real:
                if _leg_is_minimal_miko_inbound(leg, known_exts):
                    phone = (leg.get("src_num") or "").strip()
                    break

        if _digit_len(phone) < 6:
            continue

        starts = [leg.get("start") or "" for leg in legs if leg.get("start")]
        missed.append(
            {
                "linkedid": key,
                "start": min(starts) if starts else "",
                "phone": phone,
                "did": did,
                "ext": ext,
            }
        )
    return missed


def collect_missed_inbound_for_worker(
    rows: list[dict[str, Any]], known_exts: set[str]
) -> list[dict[str, Any]]:
    """Missed inbound calls for the entity worker (same set admin shows as missed inbound)."""
    by_lid: dict[str, dict[str, Any]] = {}
    for c in _extract_missed_inbound_calls(rows, known_exts):
        lid = (c.get("linkedid") or "").strip()
        if lid:
            by_lid[lid] = c
    for c in aggregate_cdr_calls(rows, known_exts):
        if (c.get("direction") or "").strip() != "incoming":
            continue
        if c.get("answered"):
            continue
        lid = (c.get("linkedid") or "").strip()
        if not lid or lid in by_lid:
            continue
        phone = (c.get("phone") or "").strip()
        if _digit_len(phone) < 6:
            continue
        by_lid[lid] = {
            "linkedid": lid,
            "start": c.get("start") or "",
            "phone": phone,
            "did": "",
            "ext": (c.get("ext") or "").removesuffix("-WS"),
        }
    return list(by_lid.values())


def extract_inbound_cdr_calls(
    rows: list[dict[str, Any]], known_exts: set[str]
) -> list[dict[str, Any]]:
    """All inbound PBX calls for admin log (answered + missed DID/IVR)."""
    by_lid: dict[str, dict[str, Any]] = {}
    for c in aggregate_cdr_calls(rows, known_exts):
        lid = (c.get("linkedid") or "").strip()
        if not lid or c.get("direction") != "incoming":
            continue
        by_lid[lid] = {
            "linkedid": lid,
            "start": c.get("start") or "",
            "phone": c.get("phone") or "",
            "ext": (c.get("ext") or "").removesuffix("-WS"),
            "did": "",
            "was_answered": bool(c.get("answered")),
            "billsec": int(c.get("billsec") or 0),
            "duration": int(c.get("duration") or 0),
        }
    for c in _extract_missed_inbound_calls(rows, known_exts):
        lid = (c.get("linkedid") or "").strip()
        if not lid or lid in by_lid:
            continue
        by_lid[lid] = {
            "linkedid": lid,
            "start": c.get("start") or "",
            "phone": c.get("phone") or "",
            "ext": (c.get("ext") or "").removesuffix("-WS"),
            "did": c.get("did") or "",
            "was_answered": False,
            "billsec": 0,
            "duration": 0,
        }
    return sorted(by_lid.values(), key=lambda x: x.get("start") or "", reverse=True)


async def _query_cdr_rows_hours(hours: int) -> list[dict[str, Any]]:
    offset = _pbx_offset_hours()
    now_utc = datetime.now(timezone.utc)
    end_local = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
    start_local = end_local - timedelta(hours=max(1, min(168, hours)))
    fmt = "%Y-%m-%d %H:%M:%S"
    start_from = start_local.strftime(fmt)
    start_to = end_local.strftime(fmt)
    if _query_cdr_fn:
        return await _query_cdr_fn(
            start_from=start_from,
            start_to=start_to,
            limit=CDR_SCAN_LIMIT,
            offset=0,
        )
    return query_cdr(
        cdr_db_path=_cfg.get("cdr_db_path") or "",
        config_db_path=_cfg.get("config_db_path") or "",
        start_from=start_from,
        start_to=start_to,
        limit=CDR_SCAN_LIMIT,
        offset=0,
        docker_container=_cfg.get("mikopbx_docker_container"),
        docker_db_path=_cfg.get("cdr_docker_db_path"),
    )


async def _query_recent_cdr_rows() -> list[dict[str, Any]]:
    start_from, start_to = _pbx_local_window(LOOKBACK_MINUTES)
    if _worker_started_at_utc:
        offset = _pbx_offset_hours()
        worker_local = (_worker_started_at_utc + timedelta(hours=offset)).replace(tzinfo=None)
        worker_from = worker_local.strftime("%Y-%m-%d %H:%M:%S")
        if worker_from > start_from:
            start_from = worker_from
    if _query_cdr_fn:
        return await _query_cdr_fn(
            start_from=start_from,
            start_to=start_to,
            limit=CDR_SCAN_LIMIT,
            offset=0,
        )
    return query_cdr(
        cdr_db_path=_cfg.get("cdr_db_path") or "",
        config_db_path=_cfg.get("config_db_path") or "",
        start_from=start_from,
        start_to=start_to,
        limit=CDR_SCAN_LIMIT,
        offset=0,
        docker_container=_cfg.get("mikopbx_docker_container"),
        docker_db_path=_cfg.get("cdr_docker_db_path"),
    )


async def _client_for_extension(extension: str) -> Optional[KommoCrmClient]:
    if not _get_session:
        return None
    ext = (extension or "").strip()
    if ext and permissions_db.is_kommo_extension_excluded(ext):
        return None
    session = await _get_session(ext)
    if not session or not session.get("access_token"):
        return None

    async def refresh_token() -> tuple[str, Optional[str]]:
        refreshed = await _get_session(ext)
        if refreshed and refreshed.get("access_token"):
            return refreshed["access_token"], refreshed.get("expires_at")
        return session["access_token"], session.get("expires_at")

    return KommoCrmClient(
        session.get("subdomain") or "",
        session["access_token"],
        account_base_url=session.get("account_base_url"),
        acting_user_id=session.get("kommo_user_id"),
        token_refresher=refresh_token,
    )


async def _process_missed_incoming(
    call: dict[str, Any],
    *,
    ignore_worker_start: bool = False,
    ignore_min_age: bool = False,
    force: bool = False,
    entity_source: str = "cdr_worker",
) -> dict[str, Any]:
    linkedid = (call.get("linkedid") or "").strip()
    phone = _normalize_phone(call.get("phone") or "")
    if not linkedid or not phone:
        return {"ok": False, "linkedid": linkedid, "reason": "missing linkedid or phone"}

    if force:
        clear_fn = getattr(kommo_jobs_db, "clear_cdr_entity_processed", None)
        if callable(clear_fn):
            clear_fn(linkedid)
        _no_rule_linkedids.discard(linkedid)
    elif _is_cdr_entity_processed(linkedid):
        return {"ok": False, "linkedid": linkedid, "reason": "already_processed"}
    elif linkedid in _no_rule_linkedids:
        return {"ok": False, "linkedid": linkedid, "reason": "no_matching_rule_cached"}

    call_start_utc = _call_start_utc(call)
    if not call_start_utc:
        return {"ok": False, "linkedid": linkedid, "reason": "missing call start time"}

    if (
        not ignore_worker_start
        and _worker_started_at_utc
        and call_start_utc < _worker_started_at_utc
    ):
        return {"ok": False, "linkedid": linkedid, "reason": "before_worker_start"}

    age_sec = (datetime.now(timezone.utc) - call_start_utc).total_seconds()
    if not ignore_min_age and age_sec < MIN_CALL_AGE_SECONDS:
        return {
            "ok": False,
            "linkedid": linkedid,
            "reason": f"call too recent ({int(age_sec)}s)",
        }

    if _cdr_call_has_active_kommo_job(
        linkedid=linkedid,
        phone=phone,
        call_time_utc=call_start_utc,
    ):
        print(
            f"[kommo_cdr_entity_worker] skip linkedid={linkedid} phone={phone}: "
            f"process-call job already active",
            flush=True,
        )
        return {"ok": False, "linkedid": linkedid, "reason": "active_process_call_job"}

    if not list_rules():
        return {"ok": False, "linkedid": linkedid, "reason": "no_entity_rules_configured"}

    extension = (call.get("ext") or "").strip().removesuffix("-WS")
    client = await _client_for_extension(extension)
    if not client:
        print(
            f"[kommo_cdr_entity_worker] skip linkedid={linkedid}: "
            f"no Kommo session (ext={extension or '-'})",
            flush=True,
        )
        return {"ok": False, "linkedid": linkedid, "reason": "no_kommo_session"}

    try:
        print(
            f"[kommo_cdr_entity_worker] missed inbound linkedid={linkedid} "
            f"phone={phone} ext={extension or '-'} age={int(age_sec)}s source={entity_source}",
            flush=True,
        )
        entity_ctx = await apply_entity_rule(
            client,
            phone=phone,
            is_incoming=True,
            was_answered=False,
            call_time=call_start_utc,
            did=(call.get("did") or "").strip(),
            call_from_label=phone,
            acting_user_id=client.get_acting_user_id(),
            linkedid=linkedid,
            entity_source=entity_source,
        )
        print(
            f"[kommo_cdr_entity_worker] done linkedid={linkedid} "
            f"rule={entity_ctx.rule_id} task={entity_ctx.task_created} "
            f"contact={entity_ctx.contact_id} lead={entity_ctx.lead_id}",
            flush=True,
        )
        if not entity_ctx.rule_id:
            _no_rule_linkedids.add(linkedid)
            return {"ok": False, "linkedid": linkedid, "reason": "no_matching_rule"}

        has_outcome = bool(
            entity_ctx.lead_id
            or entity_ctx.contact_id
            or (entity_ctx.task_created and entity_ctx.task_id)
        )
        return {
            "ok": has_outcome,
            "linkedid": linkedid,
            "reason": "" if has_outcome else "rule_matched_no_outcome",
            "rule_id": entity_ctx.rule_id,
            "call_type": entity_ctx.call_type,
            "task_created": entity_ctx.task_created,
            "task_id": entity_ctx.task_id,
            "contact_id": entity_ctx.contact_id,
            "lead_id": entity_ctx.lead_id,
        }
    finally:
        await client.close()


async def apply_missed_inbound_entities_manual(
    linkedid: str,
    *,
    hours: int = 168,
    force: bool = False,
) -> dict[str, Any]:
    """Admin manual push: apply entity rules for one inbound linkedid (any call age)."""
    lid = (linkedid or "").strip()
    if not lid:
        return {"ok": False, "reason": "missing linkedid"}
    if not list_rules():
        return {"ok": False, "linkedid": lid, "reason": "no_entity_rules_configured"}
    if not _enabled():
        return {"ok": False, "linkedid": lid, "reason": "kommo_not_configured"}
    try:
        rows = await _query_cdr_rows_hours(hours)
    except Exception as exc:
        log.exception("CDR query failed for manual entity apply: %s", exc)
        return {"ok": False, "linkedid": lid, "reason": f"cdr_query_failed: {exc}"}

    known_exts = _known_extensions()
    missed = collect_missed_inbound_for_worker(rows, known_exts)
    call = next((c for c in missed if (c.get("linkedid") or "").strip() == lid), None)
    if not call:
        return {"ok": False, "linkedid": lid, "reason": "call_not_found_or_not_missed_inbound"}

    try:
        return await _process_missed_incoming(
            call,
            ignore_worker_start=True,
            ignore_min_age=True,
            force=force,
            entity_source="admin_manual",
        )
    except Exception as exc:
        log.exception("manual entity apply failed linkedid=%s: %s", lid, exc)
        return {"ok": False, "linkedid": lid, "reason": f"{type(exc).__name__}: {exc}"}


async def _poll_once() -> None:
    if not _enabled():
        return
    try:
        rows = await _query_recent_cdr_rows()
    except Exception as exc:
        log.exception("CDR query failed: %s", exc)
        print(f"[kommo_cdr_entity_worker] CDR query error: {exc}", flush=True)
        return

    known_exts = _known_extensions()
    missed = collect_missed_inbound_for_worker(rows, known_exts)
    missed = [c for c in missed if _call_is_after_worker_start(c)]
    print(
        f"[kommo_cdr_entity_worker] scan rows={len(rows)} missed_inbound={len(missed)}",
        flush=True,
    )
    if not missed:
        return

    for call in missed:
        try:
            await _process_missed_incoming(call)
        except Exception as exc:
            log.exception(
                "entity rules failed linkedid=%s: %s",
                call.get("linkedid"),
                exc,
            )
            print(
                f"[kommo_cdr_entity_worker] error linkedid={call.get('linkedid')}: {exc}",
                flush=True,
            )


async def _loop() -> None:
    while _running:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("poll loop error: %s", exc)
            print(f"[kommo_cdr_entity_worker] poll error: {exc}", flush=True)
        try:
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            break


async def start_worker(
    *,
    cfg: dict[str, Any],
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Optional[Callable[..., Awaitable[list]]] = None,
) -> None:
    global _running, _task, _cfg, _get_session, _query_cdr_fn, _worker_started_at_utc
    configure(cfg)
    _cfg = cfg
    _get_session = get_session_for_extension
    _query_cdr_fn = query_cdr
    if _running:
        return
    if not _enabled():
        print(
            "[kommo_cdr_entity_worker] not started — Kommo disabled or not authorized",
            flush=True,
        )
        return
    _worker_started_at_utc = datetime.now(timezone.utc)
    _running = True
    _task = asyncio.create_task(_loop(), name="kommo-cdr-entity-worker")
    print(
        f"[kommo_cdr_entity_worker] started poll={POLL_SECONDS}s "
        f"min_age={MIN_CALL_AGE_SECONDS}s lookback={LOOKBACK_MINUTES}m "
        f"(only calls after service start, not historical CDR)",
        flush=True,
    )


async def stop_worker() -> None:
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


def known_extensions(cfg: dict[str, Any] | None = None) -> set[str]:
    """Extension list for CDR aggregation (admin inbound log)."""
    global _cfg
    prev = _cfg
    if cfg:
        _cfg = cfg
    try:
        return _known_extensions()
    finally:
        if cfg:
            _cfg = prev
