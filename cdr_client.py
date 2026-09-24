import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pjsip_secrets import _resolve_docker

_PHONE_CID_RE = re.compile(r"^\+?\d{5,15}$")


def _effective_outbound_callerid(trunk_id, dst_name, src_num, trunk_callerids):
    """Actual outbound CallerID presented on a trunk call.

    MikoPBX stores the effective connected-line number of the trunk leg
    in dst_name (e.g. a per-call CID forced via Dial f(...)). Prefer it
    over the static trunk fromuser, which is one number for the whole
    trunk regardless of what the call actually presented.
    """
    name = (dst_name or "").strip()
    if trunk_id and _PHONE_CID_RE.match(name):
        return name
    return trunk_callerids.get(trunk_id, src_num or "")


_CDR_CACHE_LOCK = threading.Lock()
_CDR_CACHE_HOST_PATH: str | None = None
_CDR_CACHE_VALID_UNTIL: float = 0.0
_CDR_CACHE_TTL_SEC = 2.0


def cdr_sqlite_available(
    cdr_db_path: str,
    *,
    docker_container: str | None = None,
    docker_db_path: str | None = None,
) -> bool:
    """True when Kommo/gateway can read CDR from local SQLite."""
    docker_db_path = (docker_db_path or "").strip()
    if docker_db_path:
        return bool((docker_container or "").strip())
    try:
        return Path(cdr_db_path or "").is_file()
    except OSError:
        return False


def _host_cdr_sqlite_path(
    cdr_db_path: str,
    docker_container: str,
    docker_db_path: str,
) -> str:
    """Return a host filesystem path to an SQLite file (either ``cdr_db_path`` or a temp copy from Docker)."""
    docker_db_path = (docker_db_path or "").strip()
    container = (docker_container or "").strip()
    if docker_db_path:
        if not container:
            raise FileNotFoundError(
                "cdr_docker_db_path is set but mikopbx_docker_container is empty; "
                "set the container name or clear cdr_docker_db_path."
            )
        docker_bin = _resolve_docker()
        if not docker_bin:
            raise FileNotFoundError("docker not found on PATH; cannot read cdr_docker_db_path.")

        global _CDR_CACHE_HOST_PATH, _CDR_CACHE_VALID_UNTIL
        now = time.monotonic()
        with _CDR_CACHE_LOCK:
            if (
                _CDR_CACHE_HOST_PATH
                and now < _CDR_CACHE_VALID_UNTIL
                and os.path.isfile(_CDR_CACHE_HOST_PATH)
            ):
                return _CDR_CACHE_HOST_PATH

            if _CDR_CACHE_HOST_PATH and os.path.isfile(_CDR_CACHE_HOST_PATH):
                try:
                    os.unlink(_CDR_CACHE_HOST_PATH)
                except OSError:
                    pass
                _CDR_CACHE_HOST_PATH = None

            fd, tmp = tempfile.mkstemp(prefix="mikopbx-cdr-", suffix=".db")
            os.close(fd)
            r = subprocess.run(
                [docker_bin, "cp", f"{container}:{docker_db_path}", tmp],
                capture_output=True,
                timeout=60,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise FileNotFoundError(
                    f"docker cp {container}:{docker_db_path} failed: {err or r.returncode}"
                )
            _CDR_CACHE_HOST_PATH = tmp
            _CDR_CACHE_VALID_UNTIL = now + _CDR_CACHE_TTL_SEC
            return tmp

    if not Path(cdr_db_path).exists():
        raise FileNotFoundError(f"CDR database not found: {cdr_db_path}")
    return cdr_db_path


def _extension_sql_clause(ext: str) -> tuple[str, list]:
    """Match extension on num/account/PJSIP/Local originate channels."""
    ext = ext.strip()
    clause = (
        "("
        "src_num = ? OR dst_num = ? "
        "OR from_account = ? OR to_account = ? "
        "OR src_chan LIKE '%/' || ? || '-%' OR dst_chan LIKE '%/' || ? || '-%' "
        "OR src_chan LIKE 'Local/' || ? || '@%' OR dst_chan LIKE 'Local/' || ? || '@%'"
        ")"
    )
    return clause, [ext, ext, ext, ext, ext, ext, ext, ext]


def _phone_sql_clause(phone: str, *, is_incoming: bool = False) -> tuple[str, list]:
    """Match destination/source phone with or without leading +."""
    phone = (phone or "").strip()
    digits = "".join(c for c in phone if c.isdigit())
    variants: list[str] = []
    for value in (phone, digits, f"+{digits}" if digits else ""):
        if value and value not in variants:
            variants.append(value)
    if len(digits) >= 10:
        tail = digits[-10:]
        if tail not in variants:
            variants.append(tail)
    placeholders = ", ".join("?" for _ in variants)
    field = "src_num" if is_incoming else "dst_num"
    clause = f"({field} IN ({placeholders}))"
    return clause, variants


def _row_from_sqlite(row: sqlite3.Row, trunk_callerids: dict[str, str]) -> dict:
    trunk_id = row["to_account"] or ""
    _dst_name = row["dst_name"] if "dst_name" in row.keys() else ""
    caller_id = _effective_outbound_callerid(
        trunk_id, _dst_name, row["src_num"], trunk_callerids
    )
    keys = row.keys()
    return {
        "src_num": row["src_num"] or "",
        "dst_num": row["dst_num"] or "",
        "caller_id": caller_id,
        "start": row["start"] or "",
        "answer": row["answer"] or "",
        "duration": row["duration"] or 0,
        "billsec": row["billsec"] or 0,
        "disposition": row["disposition"] or "",
        "recording": row["recordingfile"] or "",
        "linkedid": row["linkedid"] or "",
        "trunk": trunk_id,
        "src_call_id": row["src_call_id"] or "",
        "work_completed": (row["work_completed"] if "work_completed" in keys else "") or "",
        "appname": (row["appname"] if "appname" in keys else "") or "",
        "src_chan": (row["src_chan"] if "src_chan" in keys else "") or "",
        "dst_chan": (row["dst_chan"] if "dst_chan" in keys else "") or "",
        "from_account": (row["from_account"] if "from_account" in keys else "") or "",
    }


def query_cdr(
    cdr_db_path: str,
    config_db_path: str,
    ext: str | None = None,
    dst: str | None = None,
    linkedid: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    docker_container: str | None = None,
    docker_db_path: str | None = None,
    is_incoming: bool = False,
) -> list[dict]:
    """Query CDR from SQLite and resolve outbound CallerID via trunk config."""

    resolved = _host_cdr_sqlite_path(
        cdr_db_path, docker_container or "", docker_db_path or ""
    )

    trunk_callerids = _load_trunk_callerids(config_db_path)

    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where = []
        params: list = []

        if linkedid:
            where.append("linkedid = ?")
            params.append(linkedid.strip())
        if ext:
            clause, clause_params = _extension_sql_clause(ext)
            where.append(clause)
            params.extend(clause_params)
        if dst:
            clause, clause_params = _phone_sql_clause(dst, is_incoming=is_incoming)
            where.append(clause)
            params.extend(clause_params)
        if start_from:
            where.append("start >= ?")
            params.append(start_from.replace("T", " "))
        if start_to:
            where.append("start <= ?")
            params.append(start_to.replace("T", " "))

        sql = "SELECT * FROM cdr_general"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()

        return [_row_from_sqlite(row, trunk_callerids) for row in rows]
    finally:
        conn.close()


def aggregate_cdr_calls(rows: list[dict], known_exts: set[str]) -> list[dict]:
    """Collapse per-leg CDR rows into one record per call for the dashboard.

    A single call (linkedid) produces several CDR legs: the originate leg,
    Local channels, the trunk leg, queue/forward legs. The dashboard wants
    one line per call: who (extension), which way (out/in/internal), which
    external number, whether it was answered, how long it talked, and which
    device the extension used (web softphone registers as ``<ext>-WS``).
    """

    def _digits(value) -> str:
        return "".join(ch for ch in str(value or "") if ch.isdigit())

    def _is_internal(num: str) -> bool:
        n = (num or "").strip()
        if not n:
            return False
        if n in known_exts or n.removesuffix("-WS") in known_exts:
            return True
        # Fallback when the extension list is unavailable: short numeric ids
        # are internal, phone-length numbers are external.
        return not known_exts and n.isdigit() and len(n) <= 5

    def _device_for_ext(legs: list[dict], ext: str) -> str:
        if not ext:
            return "unknown"
        ws_needle = f"/{ext}-WS-"
        sip_needle = f"/{ext}-"
        found_sip = False
        for leg in legs:
            for chan in (leg.get("src_chan") or "", leg.get("dst_chan") or ""):
                if ws_needle in chan:
                    return "web"
                if sip_needle in chan and not chan.startswith("Local/"):
                    found_sip = True
        return "sip" if found_sip else "unknown"

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for i, row in enumerate(rows):
        key = (row.get("linkedid") or "").strip() or f"__row{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    calls: list[dict] = []
    for key in order:
        legs = groups[key]
        # Bootstrap legs from click-to-call are noise for party detection.
        real = [l for l in legs if (l.get("appname") or "").strip().lower() != "originate"] or legs

        ext = ""
        phone = ""
        direction = "internal"
        for leg in real:
            src, dst = (leg.get("src_num") or "").strip(), (leg.get("dst_num") or "").strip()
            src_int, dst_int = _is_internal(src), _is_internal(dst)
            if src_int and dst and not dst_int and len(_digits(dst)) >= 6:
                direction, ext, phone = "outgoing", src, dst
                break
            if dst_int and src and not src_int and len(_digits(src)) >= 6:
                direction, ext, phone = "incoming", dst, src
        if not ext:
            # No external party found: internal call (or unparseable legs).
            for leg in real:
                src, dst = (leg.get("src_num") or "").strip(), (leg.get("dst_num") or "").strip()
                if _is_internal(src):
                    ext, phone = src, dst
                    break
            else:
                first = real[0]
                ext = (first.get("src_num") or "").strip()
                phone = (first.get("dst_num") or "").strip()
            if any((l.get("trunk") or l.get("to_account") or "").strip() for l in real):
                direction = "outgoing"

        if direction == "incoming":
            # Credit the extension that actually answered, if any.
            for leg in real:
                dst = (leg.get("dst_num") or "").strip()
                if (leg.get("disposition") or "").upper() == "ANSWERED" and _is_internal(dst):
                    ext = dst
                    break

        answered = any((l.get("disposition") or "").upper() == "ANSWERED" for l in real)
        billsec = max((int(l.get("billsec") or 0) for l in real), default=0)
        duration = max((int(l.get("duration") or 0) for l in legs), default=0)
        starts = [l.get("start") or "" for l in legs if l.get("start")]
        trunk = next(
            (t for t in ((l.get("trunk") or l.get("to_account") or "").strip() for l in legs) if t),
            "",
        )
        ext = ext.removesuffix("-WS")
        calls.append({
            "linkedid": key if not key.startswith("__row") else "",
            "start": min(starts) if starts else "",
            "direction": direction,
            "ext": ext,
            "phone": phone,
            "answered": answered,
            "billsec": billsec if answered else 0,
            "duration": duration,
            "device": _device_for_ext(legs, ext),
            "trunk": trunk,
        })
    return calls


def linkedid_involves_extension(
    cdr_db_path: str,
    linkedid: str,
    extension: str,
    *,
    docker_container: str | None = None,
    docker_db_path: str | None = None,
) -> bool:
    """True if any CDR row for *linkedid* includes *extension* as src or dst."""
    linkedid = (linkedid or "").strip()
    extension = (extension or "").strip()
    if not linkedid or not extension:
        return False
    try:
        resolved = _host_cdr_sqlite_path(
            cdr_db_path, docker_container or "", docker_db_path or ""
        )
    except FileNotFoundError:
        return False
    if not Path(resolved).exists():
        return False

    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        ext_clause, ext_params = _extension_sql_clause(extension)
        row = conn.execute(
            f"SELECT 1 FROM cdr_general WHERE linkedid = ? AND {ext_clause} LIMIT 1",
            (linkedid, *ext_params),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _recording_row_is_trunk(row: sqlite3.Row) -> bool:
    keys = row.keys()
    trunk = str((row["to_account"] if "to_account" in keys else "") or "").upper()
    return "TRUNK" in trunk or trunk.startswith("SIP-")


def _recording_row_billsec(row: sqlite3.Row) -> int:
    try:
        return int(row["billsec"] or 0)
    except (TypeError, ValueError):
        return 0


def _pick_miko_recording_row(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    """Pick the PBX mixdown leg Miko intended for CDR playback (trunk on outbound)."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    trunk_rows = [r for r in rows if _recording_row_is_trunk(r)]
    pool = trunk_rows if trunk_rows else rows
    return max(pool, key=_recording_row_billsec)


def find_recording_path(
    cdr_db_path: str,
    recording_base: str,
    linkedid: str,
    *,
    docker_container: str | None = None,
    docker_db_path: str | None = None,
) -> str | None:
    """Look up the recording file path for a given linkedid.

    MikoPBX stores paths relative to its storage root (e.g.
    /storage/usbdisk1/...).  On the host the actual prefix is
    typically /var/spool/mikopbx.  We prepend `recording_base` to
    turn the DB path into an absolute host path.

    Originate / WebRTC calls often have multiple CDR legs with separate
    recordingfile values (softphone leg + trunk).  Desktop playback uses
    the trunk mix — taking ORDER BY start DESC picks the WebRTC leg and
    produces slow/wrong audio in Kommo.
    """
    resolved = _host_cdr_sqlite_path(
        cdr_db_path, docker_container or "", docker_db_path or ""
    )

    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM cdr_general "
            "WHERE linkedid = ? AND recordingfile IS NOT NULL AND recordingfile != ''",
            (linkedid,),
        ).fetchall()

        row = _pick_miko_recording_row(list(rows))
        if row is None:
            return None

        db_path = row["recordingfile"]
        for prefix in ("/storage/", "/var/spool/mikopbx/"):
            if db_path.startswith(prefix):
                db_path = db_path[len(prefix):]
                break
        full_path = Path(recording_base) / db_path.lstrip("/")
        return str(full_path)
    finally:
        conn.close()


def _load_trunk_callerids(config_db_path: str) -> dict[str, str]:
    """Load trunk fromuser (CallerID) mapping from mikopbx.db.

    Returns a dict: trunk_uniqid -> fromuser (e.g. "+12138367568").
    """
    if not Path(config_db_path).exists():
        return {}

    conn = sqlite3.connect(f"file:{config_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT uniqid, fromuser FROM m_Sip "
            "WHERE type='friend' AND fromuser IS NOT NULL AND fromuser != ''"
        ).fetchall()
        return {row["uniqid"]: row["fromuser"] for row in rows}
    except Exception:
        return {}
    finally:
        conn.close()


# =======================================================================
# REST-backed equivalents (MikoPBX REST API v3)
#
# These functions mirror the SQLite helpers above but hit the MikoPBX REST
# API instead. They are used only when ``use_rest_api`` is enabled in
# config.yaml — otherwise all CDR/trunk access stays on the legacy path.
# =======================================================================

def _match_extension(row: dict, ext: str) -> bool:
    """True when an extension participates in a REST CDR row as src or dst.

    Mirrors the SQL ``LIKE '%/ext-%'`` check we do against SQLite because
    MikoPBX doesn't tag ``src_num``/``dst_num`` for internal-originate calls
    (the extension only appears in ``src_chan``/``dst_chan``).

    WebRTC endpoints often register as ``201-WS`` while JWT carries
    ``201`` — accept both forms.
    """
    if not ext:
        return True
    variants = {ext.strip()}
    if ext.endswith("-WS"):
        variants.add(ext[:-3])
    else:
        variants.add(f"{ext}-WS")
    for e in variants:
        if not e:
            continue
        needle_chan = f"/{e}-"
        local_chan = f"Local/{e}@"
        if (
            (row.get("src_num") or "") == e
            or (row.get("dst_num") or "") == e
            or (row.get("from_account") or "") == e
            or (row.get("to_account") or "") == e
            or needle_chan in (row.get("src_chan") or "")
            or needle_chan in (row.get("dst_chan") or "")
            or (row.get("src_chan") or "").startswith(local_chan)
            or (row.get("dst_chan") or "").startswith(local_chan)
        ):
            return True
    return False


def _row_matches_phone_rest(row: dict, dst_digits: str, *, is_incoming: bool) -> bool:
    if not dst_digits:
        return True
    field = "src_num" if is_incoming else "dst_num"
    other = "dst_num" if is_incoming else "src_num"
    for key in (field, other):
        digits = _normalise_phone(row.get(key))
        if not digits:
            continue
        if digits == dst_digits:
            return True
        if len(digits) >= 10 and len(dst_digits) >= 10 and digits[-10:] == dst_digits[-10:]:
            return True
    return False


def _normalise_phone(raw: str | None) -> str:
    """Strip punctuation so ``+15551234567`` and ``15551234567`` compare equal.

    MikoPBX may store the dialled number with or without the leading ``+``
    depending on trunk and dialplan manipulation. We do a digits-only match
    on the Python side as a safety net so the client-side filter does not
    miss the right CDR because of a formatting difference.
    """
    if not raw:
        return ""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _parse_cdr_dt(value: str | None) -> Any:
    """Parse MikoPBX CDR ``start`` strings for time-window filtering.

    MikoPBX emits ``YYYY-MM-DD HH:MM:SS[.ms]`` strings. We try the common
    shapes and return ``None`` for anything unexpected so the caller can
    skip the time filter for that row instead of crashing.
    """
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def query_cdr_rest(
    rest_client: Any,
    trunk_callerids: dict[str, str],
    *,
    ext: str | None = None,
    dst: str | None = None,
    linkedid: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    max_pages: int = 5,
    is_incoming: bool = False,
) -> list[dict]:
    """CDR list via MikoPBX REST API, shaped like :func:`query_cdr`.

    MikoPBX v3 server-side filtering for ``/cdr`` is incomplete in the field:
    some builds silently ignore unknown date-format variants, others require
    an exact ``dst_num`` match (so ``+15551234567`` doesn't match a row stored
    as ``15551234567``). We therefore always fetch an over-sized page and
    enforce ``dst``/``ext``/time-range filters in Python. This costs at most
    one extra page of CDR per request and fixes the "no records found" class
    of bugs we used to hit.
    """
    # Over-fetch to give the client-side filter something to work with. Cap at
    # MikoPBX's page limit (100) so we don't trip the server.
    page_size = min(100, max(limit * 3, limit + 20))

    dst_digits = _normalise_phone(dst)
    from_dt = _parse_cdr_dt(start_from) if start_from else None
    to_dt = _parse_cdr_dt(start_to) if start_to else None
    linkedid = (linkedid or "").strip()

    async def _fetch(use_server_dates: bool) -> list[dict]:
        results: list[dict] = []
        for page in range(max(1, max_pages)):
            raw = await rest_client.list_cdr(
                limit=page_size,
                offset=offset + page * page_size,
                date_from=_iso_to_dt(start_from) if use_server_dates else None,
                date_to=_iso_to_dt(start_to) if use_server_dates else None,
            )
            if not raw:
                break

            for row in raw:
                if linkedid and (row.get("linkedid") or "") != linkedid:
                    continue
                if ext and not _match_extension(row, ext):
                    continue
                if dst_digits and not _row_matches_phone_rest(
                    row, dst_digits, is_incoming=is_incoming
                ):
                    continue
                if from_dt or to_dt:
                    row_dt = _parse_cdr_dt(row.get("start"))
                    if row_dt is not None:
                        if from_dt and row_dt < from_dt:
                            continue
                        if to_dt and row_dt > to_dt:
                            continue
                trunk_id = row.get("to_account") or ""
                caller_id = _effective_outbound_callerid(
                    trunk_id, row.get("dst_name"), row.get("src_num"), trunk_callerids
                )
                results.append({
                    "src_num": row.get("src_num") or "",
                    "dst_num": row.get("dst_num") or "",
                    "caller_id": caller_id,
                    "start": row.get("start") or "",
                    "answer": row.get("answer") or "",
                    "duration": int(row.get("duration") or 0),
                    "billsec": int(row.get("billsec") or 0),
                    "disposition": row.get("disposition") or "",
                    "recording": row.get("recordingfile") or "",
                    "linkedid": row.get("linkedid") or "",
                    "trunk": trunk_id,
                    "src_call_id": row.get("src_call_id") or "",
                    "work_completed": row.get("work_completed") or "",
                    "appname": row.get("appname") or "",
                    "src_chan": row.get("src_chan") or "",
                    "dst_chan": row.get("dst_chan") or "",
                    "from_account": row.get("from_account") or "",
                    "to_account": row.get("to_account") or row.get("to_account") or "",
                    "cdr_id": row.get("id"),
                    "playback_url": row.get("playback_url") or "",
                    "download_url": row.get("download_url") or "",
                })
                if len(results) >= limit:
                    return results

            if len(raw) < page_size:
                break
        return results

    use_server_dates = bool(start_from or start_to)
    results = await _fetch(use_server_dates)
    # Some Miko builds return an empty first page when dateFrom/dateTo is set;
    # fall back to unpaginated server dates and keep the client-side time filter.
    if not results and use_server_dates:
        results = await _fetch(False)
    return results


async def linkedid_involves_extension_rest(
    rest_client: Any,
    linkedid: str,
    extension: str,
) -> bool:
    """Check linkedid ownership via REST (paginated — not only the latest 500 rows)."""
    linkedid = (linkedid or "").strip()
    extension = (extension or "").strip()
    if not linkedid or not extension:
        return False

    legs = await fetch_cdr_legs_for_linkedid_rest(rest_client, linkedid, max_pages=40)
    return any(_match_extension(row, extension) for row in legs)


async def fetch_cdr_legs_for_linkedid_rest(
    rest_client: Any,
    linkedid: str,
    *,
    max_pages: int = 40,
) -> list[dict]:
    """Return every REST CDR row for *linkedid* (all legs of one call)."""
    linkedid = (linkedid or "").strip()
    if not linkedid:
        return []
    legs: list[dict] = []
    for page in range(max(1, max_pages)):
        rows = await rest_client.list_cdr(limit=100, offset=page * 100)
        if not rows:
            break
        legs.extend(row for row in rows if (row.get("linkedid") or "") == linkedid)
        if len(rows) < 100:
            break
    return legs


async def find_cdr_by_linkedid_rest(rest_client: Any, linkedid: str) -> dict | None:
    """Return the CDR row with a recording for *linkedid*, if any."""
    linkedid = (linkedid or "").strip()
    if not linkedid:
        return None
    for row in await fetch_cdr_legs_for_linkedid_rest(rest_client, linkedid, max_pages=40):
        if row.get("recordingfile"):
            return row
    return None


async def load_trunk_callerids_rest(rest_client: Any) -> dict[str, str]:
    """``provider_id -> fromuser`` mapping using MikoPBX REST.

    Equivalent to :func:`_load_trunk_callerids` but via the API. Requires N+1
    calls because the list endpoint only returns description/host/username —
    the ``fromuser`` (outbound CallerID) lives on the full ``Provider`` object.
    """
    providers = await rest_client.list_sip_providers()
    result: dict[str, str] = {}
    for p in providers:
        if p.get("disabled"):
            continue
        pid = (p.get("id") or "").strip()
        if not pid:
            continue
        full = await rest_client.get_sip_provider(pid)
        if not full:
            continue
        fromuser = (full.get("fromuser") or "").strip()
        if fromuser:
            result[pid] = fromuser
    return result


def pbx_local_cdr_window(
    hours: int,
    *,
    pbx_utc_offset_hours: float = 0,
) -> tuple[str, str]:
    """Return ``(start, end)`` in PBX-local ``YYYY-MM-DD HH:MM:SS`` for CDR queries."""
    from datetime import datetime, timedelta, timezone

    offset = float(pbx_utc_offset_hours or 0)
    now_utc = datetime.now(timezone.utc)
    end_local = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
    start_local = end_local - timedelta(hours=max(1, int(hours)))
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_local.strftime(fmt), end_local.strftime(fmt)


def normalize_cdr_query_window(
    start_from: str | None,
    start_to: str | None,
    *,
    pbx_utc_offset_hours: float = 0,
) -> tuple[str | None, str | None]:
    """Bound open-ended client windows; default ``to`` to PBX-local now."""
    from datetime import datetime, timedelta, timezone

    from_val = _iso_to_dt(start_from) if start_from else None
    to_val = _iso_to_dt(start_to) if start_to else None
    if from_val and not to_val:
        offset = float(pbx_utc_offset_hours or 0)
        now_utc = datetime.now(timezone.utc)
        end_local = (now_utc + timedelta(hours=offset)).replace(tzinfo=None)
        to_val = end_local.strftime("%Y-%m-%d %H:%M:%S")
    return from_val, to_val


def _iso_to_dt(value: str | None) -> str | None:
    """Coerce a client-provided ISO datetime into the MikoPBX REST format.

    MikoPBX expects ``YYYY-MM-DD HH:MM:SS``. Clients usually send ISO 8601
    (``YYYY-MM-DDTHH:MM:SS``) — we swap the ``T`` for a space and drop the
    trailing ``Z`` if present (some MikoPBX builds reject UTC markers, and we
    can't know the PBX timezone anyway — the caller must pass PBX-local time).
    Fractional seconds are preserved because MikoPBX accepts them optionally.
    """
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    # Replace the ISO T separator with a space to match MikoPBX's preferred
    # "2026-04-20 15:22:58" format. Safe no-op when the string already uses a
    # space.
    if "T" in v:
        v = v.replace("T", " ", 1)
    return v
