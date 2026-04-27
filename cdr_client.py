import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pjsip_secrets import _resolve_docker

_CDR_CACHE_LOCK = threading.Lock()
_CDR_CACHE_HOST_PATH: str | None = None
_CDR_CACHE_VALID_UNTIL: float = 0.0
_CDR_CACHE_TTL_SEC = 2.0


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


def query_cdr(
    cdr_db_path: str,
    config_db_path: str,
    ext: str | None = None,
    dst: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    docker_container: str | None = None,
    docker_db_path: str | None = None,
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

        if ext:
            ext = ext.strip()
            # MikoPBX often leaves src_num/dst_num empty; extension appears in PJSIP channels
            # (e.g. PJSIP/201-00000001) or account columns.
            where.append(
                "("
                "src_num = ? OR dst_num = ? "
                "OR from_account = ? OR to_account = ? "
                "OR src_chan LIKE '%/' || ? || '-%' OR dst_chan LIKE '%/' || ? || '-%'"
                ")"
            )
            params.extend([ext, ext, ext, ext, ext, ext])
        if dst:
            where.append("dst_num = ?")
            params.append(dst)
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

        results = []
        for row in rows:
            trunk_id = row["to_account"] or ""
            caller_id = trunk_callerids.get(trunk_id, row["src_num"] or "")

            results.append({
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
            })
        return results
    finally:
        conn.close()


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
        row = conn.execute(
            "SELECT 1 FROM cdr_general WHERE linkedid = ? AND ("
            "src_num = ? OR dst_num = ? "
            "OR from_account = ? OR to_account = ? "
            "OR src_chan LIKE '%/' || ? || '-%' OR dst_chan LIKE '%/' || ? || '-%'"
            ") LIMIT 1",
            (linkedid, extension, extension, extension, extension, extension, extension),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


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
    """
    resolved = _host_cdr_sqlite_path(
        cdr_db_path, docker_container or "", docker_db_path or ""
    )

    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT recordingfile FROM cdr_general "
            "WHERE linkedid = ? AND recordingfile IS NOT NULL AND recordingfile != '' "
            "ORDER BY start DESC LIMIT 1",
            (linkedid,),
        ).fetchone()

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
    """
    if not ext:
        return True
    needle_chan = f"/{ext}-"
    return (
        (row.get("src_num") or "") == ext
        or (row.get("dst_num") or "") == ext
        or (row.get("from_account") or "") == ext
        or (row.get("to_account") or "") == ext
        or needle_chan in (row.get("src_chan") or "")
        or needle_chan in (row.get("dst_chan") or "")
    )


def _normalise_phone(raw: str | None) -> str:
    """Strip punctuation so ``+17027325111`` and ``17027325111`` compare equal.

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
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """CDR list via MikoPBX REST API, shaped like :func:`query_cdr`.

    MikoPBX v3 server-side filtering for ``/cdr`` is incomplete in the field:
    some builds silently ignore unknown date-format variants, others require
    an exact ``dst_num`` match (so ``+17027325111`` doesn't match a row stored
    as ``17027325111``). We therefore always fetch an over-sized page and
    enforce ``dst``/``ext``/time-range filters in Python. This costs at most
    one extra page of CDR per request and fixes the "no records found" class
    of bugs we used to hit.
    """
    # Over-fetch to give the client-side filter something to work with. Cap at
    # MikoPBX's page limit (100) so we don't trip the server.
    fetch_limit = min(100, max(limit * 3, limit + 20))

    raw = await rest_client.list_cdr(
        limit=fetch_limit,
        offset=offset,
        date_from=_iso_to_dt(start_from),
        date_to=_iso_to_dt(start_to),
        # Intentionally not passing ``dst_num`` to MikoPBX: its server-side
        # filter is brittle (see docstring). Python-side filtering below does
        # the right thing regardless of how the PBX stored the number.
    )

    dst_digits = _normalise_phone(dst)
    # Accept the caller's ISO window verbatim for the in-memory filter so we
    # still honour it even when MikoPBX didn't.
    from_dt = _parse_cdr_dt(start_from) if start_from else None
    to_dt = _parse_cdr_dt(start_to) if start_to else None

    results: list[dict] = []
    for row in raw:
        if ext and not _match_extension(row, ext):
            continue
        if dst_digits and _normalise_phone(row.get("dst_num")) != dst_digits:
            continue
        if from_dt or to_dt:
            row_dt = _parse_cdr_dt(row.get("start"))
            if row_dt is not None:
                if from_dt and row_dt < from_dt:
                    continue
                if to_dt and row_dt > to_dt:
                    continue
            # If we couldn't parse the row's time but the caller asked for a
            # window, include the row anyway — better to return a possibly
            # useful record than to silently drop it.
        trunk_id = row.get("to_account") or ""
        caller_id = trunk_callerids.get(trunk_id, row.get("src_num") or "")
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
            # REST-only extras that make recording playback trivial for clients
            # which want to bypass the proxy and hit MikoPBX directly.
            "cdr_id": row.get("id"),
            "playback_url": row.get("playback_url") or "",
            "download_url": row.get("download_url") or "",
        })
        if len(results) >= limit:
            break
    return results


async def linkedid_involves_extension_rest(
    rest_client: Any,
    linkedid: str,
    extension: str,
) -> bool:
    """Look up ``linkedid`` in recent CDR via REST and check for the extension.

    We scan the last few CDR pages: in practice a recording that's being
    accessed is always recent. Fall back to ``False`` when nothing matches —
    the caller turns that into a 404.
    """
    linkedid = (linkedid or "").strip()
    extension = (extension or "").strip()
    if not linkedid or not extension:
        return False

    # 5 pages * 100 rows = 500 most recent CDRs. Anything older than that is
    # unlikely to be fetched through the proxy and ACLs aren't worth slowing
    # the happy path.
    for page in range(5):
        rows = await rest_client.list_cdr(limit=100, offset=page * 100)
        if not rows:
            return False
        for row in rows:
            if (row.get("linkedid") or "") == linkedid and _match_extension(row, extension):
                return True
        if len(rows) < 100:
            return False
    return False


async def find_cdr_by_linkedid_rest(rest_client: Any, linkedid: str) -> dict | None:
    """Return the most-recent CDR list item with a given linkedid (REST)."""
    linkedid = (linkedid or "").strip()
    if not linkedid:
        return None
    for page in range(5):
        rows = await rest_client.list_cdr(limit=100, offset=page * 100)
        if not rows:
            return None
        for row in rows:
            if (row.get("linkedid") or "") == linkedid and row.get("recordingfile"):
                return row
        if len(rows) < 100:
            return None
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
