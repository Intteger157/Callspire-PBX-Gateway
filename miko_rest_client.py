"""Async client for MikoPBX REST API v3.

Used when ``use_rest_api: true`` in config.yaml. Authenticates with a long-lived
API key (preferred) or admin login/password (auto-refreshed JWT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class MikoRestError(Exception):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


@dataclass
class MikoRestConfig:
    base_url: str
    api_key: str = ""
    login: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout_seconds: float = 15.0


class MikoRestClient:
    API_PREFIX = "/pbxcore/api/v3"

    def __init__(self, config: MikoRestConfig):
        self._cfg = config
        self._base = (config.base_url or "").strip().rstrip("/")
        self._access_token = (config.api_key or "").strip()
        self._refresh_token = ""
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        if not self._base:
            return False
        return bool(self._access_token or (self._cfg.login and self._cfg.password))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self._cfg.verify_ssl,
                timeout=httpx.Timeout(self._cfg.timeout_seconds),
                follow_redirects=True,
            )
        return self._client

    def _api_url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        if self._base.endswith(self.API_PREFIX):
            return f"{self._base}{path}"
        return f"{self._base}{self.API_PREFIX}{path}"

    async def _auth_headers(self) -> dict[str, str]:
        if not self._access_token and self._cfg.login and self._cfg.password:
            await self._login_with_password()
        if not self._access_token:
            raise MikoRestError("MikoPBX REST client is not authenticated")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _login_with_password(self) -> None:
        client = await self._get_client()
        url = self._api_url("/auth:login")
        r = await client.post(
            url,
            json={"login": self._cfg.login, "password": self._cfg.password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if r.status_code >= 400:
            raise MikoRestError(
                f"auth:login failed ({r.status_code})",
                status=r.status_code,
                payload=_safe_json(r),
            )
        data = _safe_json(r) or {}
        token = (
            data.get("access_token")
            or data.get("accessToken")
            or data.get("token")
            or ""
        )
        if not token:
            raise MikoRestError("auth:login did not return access token", payload=data)
        self._access_token = str(token).strip()
        self._refresh_token = str(
            data.get("refresh_token") or data.get("refreshToken") or ""
        ).strip()

    async def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            if self._cfg.login and self._cfg.password:
                await self._login_with_password()
                return True
            return False
        client = await self._get_client()
        url = self._api_url("/auth:refresh")
        r = await client.post(
            url,
            json={"refresh_token": self._refresh_token},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if r.status_code >= 400:
            if self._cfg.login and self._cfg.password:
                await self._login_with_password()
                return True
            return False
        data = _safe_json(r) or {}
        token = data.get("access_token") or data.get("accessToken") or data.get("token")
        if token:
            self._access_token = str(token).strip()
            return True
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        retry_auth: bool = True,
    ) -> Any:
        client = await self._get_client()
        headers = await self._auth_headers()
        url = self._api_url(path)
        r = await client.request(method, url, headers=headers, params=params, json=json_body)
        if r.status_code == 401 and retry_auth:
            if await self._refresh_access_token():
                headers = await self._auth_headers()
                r = await client.request(method, url, headers=headers, params=params, json=json_body)
        if r.status_code >= 400:
            payload = _safe_json(r)
            msg = _extract_error_message(payload) or r.reason_phrase or f"HTTP {r.status_code}"
            raise MikoRestError(msg, status=r.status_code, payload=payload)
        if r.status_code == 204 or not (r.content or b"").strip():
            return None
        ct = (r.headers.get("content-type") or "").lower()
        if "application/json" in ct:
            return r.json()
        return r.text

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/system:checkAuth")
            return True
        except MikoRestError:
            try:
                await self._request("GET", "/cdr:getMetadata")
                return True
            except MikoRestError:
                return False

    async def list_cdr(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        dst_num: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if dst_num:
            params["dst_num"] = dst_num
        data = await self._request("GET", "/cdr", params=params)
        return _unwrap_list(data)

    async def list_extensions_for_select(self, *, type_: str = "SIP") -> list[dict]:
        data = await self._request("GET", "/extensions:getForSelect", params={"type": type_})
        return _unwrap_list(data)

    async def list_sip_providers(self) -> list[dict]:
        data = await self._request("GET", "/sip-providers")
        return _unwrap_list(data)

    async def get_sip_provider(self, provider_id: str) -> dict | None:
        pid = (provider_id or "").strip()
        if not pid:
            return None
        data = await self._request("GET", f"/sip-providers/{pid}")
        return data if isinstance(data, dict) else None

    async def get_sip_provider_statuses(self) -> list[dict]:
        data = await self._request("GET", "/sip-providers:getStatuses")
        return _unwrap_provider_statuses(data)

    async def get_active_calls(self) -> list[dict]:
        data = await self._request("GET", "/pbx-status:getActiveCalls")
        return _unwrap_list(data)

    async def get_active_channels(self) -> list[dict]:
        data = await self._request("GET", "/pbx-status:getActiveChannels")
        return _unwrap_list(data)

    async def get_sip_peers_statuses(self) -> list[dict]:
        data = await self._request("GET", "/sip:getStatuses")
        return _unwrap_list(data)

    async def force_sip_status_check(self) -> bool:
        data = await self._request("POST", "/sip:forceCheck")
        if data is None:
            return True
        if isinstance(data, dict):
            return bool(data.get("success", True))
        return True

    async def get_sip_secret(self, sip_id: str) -> str | None:
        sid = (sip_id or "").strip()
        if not sid:
            return None
        data = await self._request("GET", f"/sip/{sid}:getSecret")
        if isinstance(data, dict):
            for key in ("secret", "password", "value"):
                val = data.get(key)
                if val:
                    return str(val)
        if isinstance(data, str):
            return data
        return None

    async def get_sip_auth_failure_stats(self) -> Any:
        try:
            return await self._request("GET", "/sip:getAuthFailureStats")
        except MikoRestError as exc:
            if exc.status in (404, 501):
                return None
            raise

    async def stream_cdr_playback(self, playback_url: str) -> httpx.Response:
        client = await self._get_client()
        headers = await self._auth_headers()
        url = playback_url.strip()
        if url.startswith("/"):
            url = self._api_url(url)
        elif not urlparse(url).scheme:
            url = urljoin(f"{self._base}/", url.lstrip("/"))
        r = await client.stream("GET", url, headers=headers)
        if r.status_code >= 400:
            body = await r.aread()
            await r.aclose()
            raise MikoRestError(
                f"playback failed ({r.status_code})",
                status=r.status_code,
                payload=body.decode(errors="replace")[:500],
            )
        return r


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return None


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "title"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                return "; ".join(str(x) for x in val)
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _unwrap_list(data: Any) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "result", "records", "rows"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
    return []


def _unwrap_provider_statuses(data: Any) -> list[dict]:
    """Parse ``/sip-providers:getStatuses`` — usually ``{data: {sip: {id: {...}}}}``."""
    if data is None:
        return []
    root = data
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        root = data["data"]
    if isinstance(root, dict):
        sip_map = root.get("sip")
        if isinstance(sip_map, dict):
            out: list[dict] = []
            for pid, info in sip_map.items():
                if not isinstance(info, dict):
                    continue
                row = dict(info)
                row.setdefault("id", str(pid).strip())
                out.append(row)
            return out
    rows = _unwrap_list(data)
    for row in rows:
        if isinstance(row, dict) and not row.get("id"):
            uid = row.get("uniqid") or row.get("provider_id")
            if uid:
                row["id"] = str(uid).strip()
    return rows
