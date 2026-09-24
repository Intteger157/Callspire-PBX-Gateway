"""Minimal Kommo CRM v4 client for server-side recording upload."""

from __future__ import annotations

import re
from typing import Any

import httpx


class KommoCrmError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D", "", (value or "").strip())


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


async def find_entity_by_phone(
    account_base_url: str,
    access_token: str,
    phone: str,
) -> dict[str, Any] | None:
    query = normalize_phone(phone)
    if not query:
        return None

    base = account_base_url.rstrip("/")
    for kind in ("contacts", "leads", "companies"):
        url = f"{base}/api/v4/{kind}?query={query}&limit=1"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=_headers(access_token))
        if response.status_code != 200:
            continue
        payload = response.json()
        items = (payload.get("_embedded") or {}).get(kind) or []
        entity_id = items[0].get("id") if items else None
        if isinstance(entity_id, int):
            return {"entity_type": kind, "entity_id": entity_id}
    return None


async def upload_file(
    account_base_url: str,
    access_token: str,
    *,
    file_name: str,
    content: bytes,
    content_type: str = "audio/mpeg",
) -> str:
    base = account_base_url.rstrip("/")
    files = {"file": (file_name, content, content_type)}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{base}/api/v4/files",
            headers=_headers(access_token),
            files=files,
        )
    if response.status_code not in (200, 201):
        raise KommoCrmError(response.status_code, response.text[:200] or "Kommo file upload failed")
    payload = response.json()
    uuid = ((payload.get("_embedded") or {}).get("files") or [{}])[0].get("uuid")
    if not (uuid or "").strip():
        raise KommoCrmError(response.status_code, "Kommo file upload returned no uuid")
    return str(uuid).strip()


async def create_call(
    account_base_url: str,
    access_token: str,
    *,
    uniq: str,
    phone: str,
    duration: int,
    direction: str,
    file_uuid: str,
    kommo_user_id: int | None,
) -> None:
    import time

    base = account_base_url.rstrip("/")
    now = int(time.time())
    body = [
        {
            "uniq": uniq,
            "phone": phone,
            "duration": max(0, int(duration)),
            "source": "intermark_softphone",
            "link": "",
            "direction": direction,
            "call_status": 4,
            "call_result": None,
            "created_at": now,
            "created_by": kommo_user_id,
            "responsible_user_id": kommo_user_id,
            "media_file": {"type": "file", "file_uuid": file_uuid},
        }
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base}/api/v4/calls",
            headers={**_headers(access_token), "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code not in (200, 201, 204):
        raise KommoCrmError(response.status_code, response.text[:200] or "Kommo call create failed")
