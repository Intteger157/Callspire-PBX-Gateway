"""Push PBX call recordings to Kommo via gateway OAuth (mobile + desktop parity)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from cdr_client import (
    find_cdr_by_linkedid,
    find_cdr_by_linkedid_rest,
    find_recording_path,
    linkedid_involves_extension,
    linkedid_involves_extension_rest,
)
from kommo_crm_client import create_call, find_entity_by_phone, upload_file
from kommo_service import get_client_session
from miko_rest_client import MikoRestClient


def _remote_number_for_extension(cdr_row: dict[str, Any], extension: str) -> str:
    src = str(cdr_row.get("src") or cdr_row.get("src_num") or "").strip()
    dst = str(cdr_row.get("dst") or cdr_row.get("dst_num") or "").strip()
    ext = extension.strip()
    if src == ext or src.endswith(ext):
        return dst or src
    if dst == ext or dst.endswith(ext):
        return src or dst
    return dst or src


def _call_direction(cdr_row: dict[str, Any], extension: str) -> str:
    src = str(cdr_row.get("src") or cdr_row.get("src_num") or "").strip()
    ext = extension.strip()
    if src == ext or src.endswith(ext):
        return "outbound"
    return "inbound"


def _duration_seconds(cdr_row: dict[str, Any]) -> int:
    for key in ("billsec", "duration"):
        raw = cdr_row.get(key)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


async def _load_recording_bytes(
    *,
    cfg: dict[str, Any],
    linkedid: str,
    cdr_row: dict[str, Any],
    miko_rest: MikoRestClient,
    rest_enabled: bool,
    cdr_docker_kwargs: Callable[[], dict],
) -> tuple[bytes, str]:
    if rest_enabled and miko_rest.enabled:
        playback_url = (cdr_row.get("playback_url") or "").strip()
        if not playback_url:
            raise ValueError("Recording playback URL missing in CDR")
        upstream = await miko_rest.stream_cdr_playback(playback_url)
        try:
            chunks: list[bytes] = []
            async for part in upstream.aiter_bytes():
                chunks.append(part)
        finally:
            await upstream.aclose()
        content = b"".join(chunks)
        if not content:
            raise ValueError("Recording file is empty")
        name = Path((cdr_row.get("recordingfile") or linkedid).split("/")[-1]).name or f"{linkedid}.mp3"
        return content, name

    file_path = find_recording_path(
        cfg["cdr_db_path"],
        cfg.get("recording_base", "/var/spool/mikopbx"),
        linkedid,
        **cdr_docker_kwargs(),
    )
    if not file_path:
        raise ValueError("Recording file path not found")
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"Recording file not found on disk: {path.name}")
    content = path.read_bytes()
    if not content:
        raise ValueError("Recording file is empty")
    return content, path.name


async def push_call_recording_to_kommo(
    *,
    cfg: dict[str, Any],
    jwt_secret: str,
    extension: str,
    linkedid: str,
    idempotency_key: str,
    miko_rest: MikoRestClient,
    rest_enabled: bool,
    cdr_docker_kwargs: Callable[[], dict],
    remote_number: str | None = None,
) -> dict[str, Any]:
    linkedid = (linkedid or "").strip()
    if not linkedid:
        raise ValueError("linkedid is required")

    session = await get_client_session(jwt_secret=jwt_secret, extension=extension)

    if rest_enabled and miko_rest.enabled:
        allowed = await linkedid_involves_extension_rest(miko_rest, linkedid, extension)
        if not allowed:
            raise ValueError("Recording not found for this extension")
        cdr_row = await find_cdr_by_linkedid_rest(miko_rest, linkedid)
    else:
        if not linkedid_involves_extension(
            cfg["cdr_db_path"],
            linkedid,
            extension,
            **cdr_docker_kwargs(),
        ):
            raise ValueError("Recording not found for this extension")
        cdr_row = find_cdr_by_linkedid(cfg["cdr_db_path"], linkedid, **cdr_docker_kwargs())

    if not cdr_row:
        raise ValueError("CDR row not found for linkedid")

    phone = (remote_number or _remote_number_for_extension(cdr_row, extension)).strip()
    if not phone:
        raise ValueError("Could not resolve remote phone from CDR")

    content, file_name = await _load_recording_bytes(
        cfg=cfg,
        linkedid=linkedid,
        cdr_row=cdr_row,
        miko_rest=miko_rest,
        rest_enabled=rest_enabled,
        cdr_docker_kwargs=cdr_docker_kwargs,
    )

    entity = await find_entity_by_phone(
        session["account_base_url"],
        session["access_token"],
        phone,
    )

    file_uuid = await upload_file(
        session["account_base_url"],
        session["access_token"],
        file_name=file_name,
        content=content,
    )

    await create_call(
        session["account_base_url"],
        session["access_token"],
        uniq=(idempotency_key or linkedid).strip(),
        phone=phone,
        duration=_duration_seconds(cdr_row),
        direction=_call_direction(cdr_row, extension),
        file_uuid=file_uuid,
        kommo_user_id=session.get("kommo_user_id"),
    )

    return {
        "ok": True,
        "file_uuid": file_uuid,
        "entity_label": (
            f"{entity['entity_type']}#{entity['entity_id']}" if entity else None
        ),
        "linkedid": linkedid,
    }
