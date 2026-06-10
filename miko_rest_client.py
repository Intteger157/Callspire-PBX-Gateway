"""Thin async HTTP client for MikoPBX REST API v3.

Auth model (per MikoPBX v2025+ docs):
    - Preferred: long-lived **API key** created in the web UI
      (Settings -> API Keys). Send it verbatim as ``Authorization: Bearer <key>``.
    - Fallback: password login via ``POST /pbxcore/api/v3/auth:login`` and
      transparent refresh via ``POST /pbxcore/api/v3/auth:refresh`` when the
      access token expires (access tokens live ~15 minutes).

Behavioural notes:
    - All MikoPBX responses follow the shape
      ``{"result": true|false, "data": ..., "messages": {...}, ...}``.
      We raise :class:`MikoRestError` when ``result`` is false or the HTTP
      status is not 2xx.
    - A single 401 automatically triggers a re-login/refresh and one retry.
    - Everything is ``async`` so it plugs straight into FastAPI endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

_log = logging.getLogger("miko_rest")


class MikoRestError(RuntimeError):
    """Raised when a MikoPBX REST call fails at HTTP or payload level."""

    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.payload = payload


@dataclass
class MikoRestConfig:
    """Configuration for :class:`MikoRestClient`. Read from ``config.yaml``."""

    base_url: str = ""
    # Preferred: static API key (JWT generated in MikoPBX web UI).
    api_key: str = ""
    # Fallback: admin credentials if no API key is configured.
    login: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout_seconds: float = 15.0
    # Path prefix for the REST API. Hardcoded in MikoPBX today, but left configurable
    # in case a reverse proxy mounts the UI under a different prefix (e.g. /tool).
    api_prefix: str = "/pbxcore/api/v3"


@dataclass
class _SessionTokens:
    access_token: str = ""
    refresh_token: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MikoRestClient:
    """Async client for a subset of MikoPBX REST v3 that the proxy needs.

    The client is safe to share across a FastAPI app: a single ``httpx.AsyncClient``
    is created on first use and reused. A per-instance asyncio lock serialises
    login/refresh so we never send two parallel re-logins on 401.
    """

    def __init__(self, cfg: MikoRestConfig) -> None:
        self._cfg = cfg
        self._tokens = _SessionTokens()
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    # ---------- lifecycle ----------

    @property
    def enabled(self) -> bool:
        """True when enough config is present to make real HTTP calls."""
        if not (self._cfg.base_url or "").strip():
            return False
        if (self._cfg.api_key or "").strip():
            return True
        return bool((self._cfg.login or "").strip() and self._cfg.password)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._cfg.base_url.rstrip("/"),
                    timeout=self._cfg.timeout_seconds,
                    verify=self._cfg.verify_ssl,
                    # Don't follow redirects by default — MikoPBX auth redirects
                    # would otherwise hide 401/403 responses.
                    follow_redirects=False,
                )
        return self._client

    # ---------- auth ----------

    async def _bearer_token(self) -> str:
        """Return the auth token to use; log in if we don't have one yet."""
        api_key = (self._cfg.api_key or "").strip()
        if api_key:
            return api_key
        if self._tokens.access_token:
            return self._tokens.access_token
        await self._login()
        return self._tokens.access_token

    async def _login(self) -> None:
        async with self._tokens.lock:
            if self._tokens.access_token and not self._cfg.api_key:
                # Someone else already refreshed while we were waiting.
                return
            client = await self._get_client()
            body = {
                "login": self._cfg.login,
                "password": self._cfg.password,
                "rememberMe": False,
            }
            resp = await client.post(
                f"{self._cfg.api_prefix}/auth:login",
                json=body,
            )
            data = self._extract_payload(resp)
            # Response: { result, data: { accessToken, refreshToken?, expiresIn, ... } }
            inner = (data or {}).get("data") or {}
            access = (inner.get("accessToken") or "").strip()
            if not access:
                raise MikoRestError(
                    resp.status_code,
                    "MikoPBX auth:login succeeded but no accessToken in response",
                    data,
                )
            self._tokens.access_token = access
            # Refresh token is ALSO stored in an httpOnly cookie by MikoPBX, but
            # we keep the value explicitly so we can refresh without cookie mgmt.
            refresh = (inner.get("refreshToken") or "").strip()
            if refresh:
                self._tokens.refresh_token = refresh
            _log.info("MikoPBX REST login succeeded (user=%s)", self._cfg.login)

    async def _refresh(self) -> bool:
        """Try to refresh the access token using the stored refresh token.

        Returns ``True`` when refresh succeeded and the access token was
        rotated. When it fails we fall back to a full re-login via :meth:`_login`.
        """
        if not self._tokens.refresh_token:
            return False
        async with self._tokens.lock:
            client = await self._get_client()
            resp = await client.post(
                f"{self._cfg.api_prefix}/auth:refresh",
                json={"refreshToken": self._tokens.refresh_token},
            )
            if resp.status_code >= 400:
                return False
            try:
                data = self._extract_payload(resp)
            except MikoRestError:
                return False
            inner = (data or {}).get("data") or {}
            new_access = (inner.get("accessToken") or "").strip()
            if not new_access:
                return False
            self._tokens.access_token = new_access
            new_refresh = (inner.get("refreshToken") or "").strip()
            if new_refresh:
                self._tokens.refresh_token = new_refresh
            return True

    # ---------- low-level request ----------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        raw: bool = False,
    ) -> Any:
        """Send an authenticated request.

        A single 401 triggers one silent refresh/re-login + retry; any further
        failure is surfaced as :class:`MikoRestError`.
        """
        if not self.enabled:
            raise MikoRestError(0, "MikoPBX REST client is not configured")

        url = f"{self._cfg.api_prefix}{path}"
        client = await self._get_client()

        async def _send(token: str) -> httpx.Response:
            return await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )

        token = await self._bearer_token()
        resp = await _send(token)

        if resp.status_code == 401 and not (self._cfg.api_key or "").strip():
            # Access token likely expired. Try refresh, else full re-login.
            if not await self._refresh():
                await self._login()
            token = await self._bearer_token()
            resp = await _send(token)

        if raw:
            return resp
        return self._extract_payload(resp)

    @staticmethod
    def _extract_payload(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            msg = _first_error_message(payload) or f"HTTP {resp.status_code}"
            raise MikoRestError(resp.status_code, msg, payload)

        try:
            payload = resp.json()
        except ValueError as exc:  # pragma: no cover - MikoPBX always returns JSON
            raise MikoRestError(
                resp.status_code,
                f"Non-JSON response from MikoPBX: {exc}",
                resp.text,
            ) from exc

        if isinstance(payload, dict) and payload.get("result") is False:
            msg = _first_error_message(payload) or "MikoPBX returned result=false"
            raise MikoRestError(resp.status_code, msg, payload)
        return payload

    # ---------- public convenience methods ----------

    async def ping(self) -> bool:
        """Cheap liveness check used on startup to log a clear warning early."""
        try:
            # ``extensions:getForSelect`` exists on every MikoPBX install and is
            # a lot cheaper than pulling a full CDR page just to test auth.
            await self._request(
                "GET",
                "/extensions:getForSelect",
                params={"type": "SIP"},
            )
            return True
        except MikoRestError as exc:
            _log.warning("MikoPBX REST ping failed: %s", exc)
            return False

    # ---- CDR ----

    async def list_cdr(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        src_num: str | None = None,
        dst_num: str | None = None,
        disposition: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
        }
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if src_num:
            params["src_num"] = src_num
        if dst_num:
            params["dst_num"] = dst_num
        if disposition:
            params["disposition"] = disposition
        payload = await self._request("GET", "/cdr", params=params)
        rows = _coerce_row_list((payload or {}).get("data"))
        # MikoPBX groups CDR by ``linkedid``: the outer array is a list of call
        # "sessions", and each session carries its real Asterisk rows in a
        # nested ``records`` array. Flatten to match the SQLite-shaped output
        # the rest of the proxy expects. We also propagate the group-level
        # ``src_name``/``dst_name`` onto nested rows (MikoPBX puts names on the
        # group but not every inner row).
        flat: list[dict] = []
        for item in rows:
            nested = item.get("records") if isinstance(item, dict) else None
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                for r in nested:
                    if not isinstance(r, dict):
                        continue
                    if not r.get("src_name") and item.get("src_name"):
                        r["src_name"] = item.get("src_name")
                    if not r.get("dst_name") and item.get("dst_name"):
                        r["dst_name"] = item.get("dst_name")
                    flat.append(r)
            else:
                flat.append(item)
        return flat

    async def get_cdr_by_id(self, cdr_id: int | str) -> dict | None:
        payload = await self._request("GET", f"/cdr/{cdr_id}")
        data = (payload or {}).get("data")
        return data if isinstance(data, dict) else None

    async def stream_cdr_playback(self, playback_url: str) -> httpx.Response:
        """Return a streaming response for a ``playback_url`` from a CDR item.

        MikoPBX includes a signed, one-shot URL in ``playback_url`` on CDR list
        items. We reuse the same authenticated client so hostname resolution,
        TLS and credentials are consistent with other REST calls.
        """
        client = await self._get_client()
        # playback_url comes back as an absolute path like
        # "/pbxcore/api/v3/cdr/12345:playback?token=...", but some deployments
        # behind reverse proxies may prepend a prefix. Pass as-is; httpx will
        # attach it to base_url.
        token = await self._bearer_token()
        req = client.build_request(
            "GET",
            playback_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = await client.send(req, stream=True)
        if resp.status_code >= 400:
            await resp.aclose()
            raise MikoRestError(resp.status_code, "Failed to open recording stream")
        return resp

    # ---- extensions ----

    async def list_extensions_for_select(self, *, type_: str = "SIP") -> list[dict]:
        """Return extensions in the "select/dropdown" shape MikoPBX uses.

        Unlike the plain ``/extensions`` list, this endpoint annotates each
        item with a high-level ``type`` (``USER``, ``CONFERENCE``, ``QUEUE``,
        ``IVR_MENU``, ``DIALPLAN_APPLICATION``, ``SYSTEM``), the localised
        group name (``typeLocalized``), and a pre-rendered display ``name``
        that contains both the employee name and the extension in angle
        brackets (e.g. ``Smith James <201>``). We use it for the admin-panel
        People/System split because the flat ``/extensions`` list has no type
        information at all.

        MikoPBX pagination on this endpoint is a single page today, but we
        still loop defensively — the server returns ``data`` as a plain list.
        """
        payload = await self._request(
            "GET",
            "/extensions:getForSelect",
            params={"type": type_},
        )
        return _coerce_row_list((payload or {}).get("data"))

    async def list_extensions(self, *, type_: str = "SIP") -> list[dict]:
        """Return all extensions of a given type (one big page).

        MikoPBX caps ``limit`` at 100; 99% of installs have < 100 extensions,
        but we paginate defensively so a large company doesn't see truncation.
        """
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            payload = await self._request(
                "GET",
                "/extensions",
                params={"type": type_, "limit": limit, "offset": offset},
            )
            page = _coerce_row_list((payload or {}).get("data"))
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
            if offset > 5000:  # hard safety limit against infinite loops
                break
        return out

    # ---- SIP providers / trunks ----

    async def list_sip_providers(self) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            payload = await self._request(
                "GET",
                "/sip-providers",
                params={"limit": limit, "offset": offset},
            )
            page = _coerce_row_list((payload or {}).get("data"))
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
            if offset > 2000:
                break
        return out

    async def get_sip_provider(self, provider_id: str) -> dict | None:
        payload = await self._request("GET", f"/sip-providers/{provider_id}")
        data = (payload or {}).get("data")
        return data if isinstance(data, dict) else None

    async def get_sip_provider_statuses(self) -> list[dict]:
        payload = await self._request("GET", "/sip-providers:getStatuses")
        data = (payload or {}).get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Some MikoPBX builds return a map keyed by provider id.
            return [{"id": k, **(v if isinstance(v, dict) else {"state": v})} for k, v in data.items()]
        return []

    # ---- SIP peers (combined trunks + user devices) ----

    async def get_sip_peers_statuses(self) -> list[dict]:
        """Live registration state for every SIP peer (both users and trunks).

        MikoPBX returns an array of objects shaped roughly like::

            { "id": "201", "state": "OK", "time_response": "15", "ip": "..." }

        The ``state`` values we care about are ``OK``/``Reachable`` (good),
        ``Lagged`` (slow but alive), ``UNREACHABLE``/``Unknown`` (dead), and
        ``OFF`` (peer is administratively disabled). Field names vary between
        MikoPBX builds — callers should look for ``state`` first and fall back
        to ``status`` or ``reg_state``.

        Endpoint name differs between MikoPBX builds: older installs expose
        ``sip:getSipPeersStatuses`` while 2024+ builds renamed it to
        ``sip:getPeersStatuses``. We try the shorter name first and fall back
        to the longer one on 404 so the widget works on both.
        """
        last_error: MikoRestError | None = None
        for path in ("/sip:getPeersStatuses", "/sip:getSipPeersStatuses"):
            try:
                payload = await self._request("GET", path)
            except MikoRestError as exc:
                if exc.status == 404:
                    last_error = exc
                    continue
                raise
            data = (payload or {}).get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict):
                return [
                    {"id": k, **(v if isinstance(v, dict) else {"state": v})}
                    for k, v in data.items()
                ]
            return []
        # Both names 404'd — surface the last error so the admin UI can see it.
        if last_error is not None:
            raise last_error
        return []

    async def get_sip_peer_status(self, peer_id: str) -> dict | None:
        """Status for a single SIP peer (one extension or trunk)."""
        last_error: MikoRestError | None = None
        for path, param in (
            ("/sip:getPeerStatus", "peer"),
            ("/sip:getPeerStatus", "id"),
            ("/sip:getSipPeer", "peer"),
        ):
            try:
                payload = await self._request("GET", path, params={param: peer_id})
            except MikoRestError as exc:
                if exc.status in (400, 404):
                    last_error = exc
                    continue
                raise
            data = (payload or {}).get("data")
            if isinstance(data, dict):
                return data
            return None
        if last_error is not None:
            raise last_error
        return None

    async def force_sip_status_check(self) -> bool:
        """Tell MikoPBX to re-probe all SIP peers immediately (sends OPTIONS).

        Returns ``True`` when the server accepted the command. MikoPBX builds
        disagree on the HTTP verb: newer REST stacks allow ``POST`` only
        (``GET`` → *not allowed with HTTP GET*); older ones only ``GET``. We
        try ``POST`` first, then ``GET``.
        """
        last_error: MikoRestError | None = None
        for method, json_body in (("POST", {}), ("GET", None)):
            try:
                payload = await self._request(
                    method,
                    "/sip:forceStatusCheck",
                    json_body=json_body,
                )
                return bool((payload or {}).get("result"))
            except MikoRestError as exc:
                msg = exc.message or ""
                if "forceStatusCheck" in msg and "not allowed with HTTP" in msg:
                    last_error = exc
                    continue
                raise
        if last_error is not None:
            raise last_error
        return False

    # ---- SIP auth failures (Fail2Ban early warning) ----

    async def get_sip_auth_failure_stats(self) -> dict:
        """Return MikoPBX's rolling window of SIP auth failures.

        The payload typically contains a ``stats`` / ``failures`` list per
        username/IP plus aggregate counters. Shapes differ slightly across
        MikoPBX builds, so callers should treat the result as best-effort.
        """
        payload = await self._request("GET", "/sip:getSipAuthFailureStats")
        data = (payload or {}).get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"failures": data}
        return {}

    # ---- SIP secret (auto-provisioning of WPF clients) ----

    async def get_sip_secret(self, username: str) -> str:
        """Return the plaintext SIP secret for ``<username>`` (extension or trunk).

        MikoPBX stores SIP secrets XOR-encrypted in the SQLite config DB but
        the REST endpoint decrypts them server-side. The response shape is
        ``{"data": {"secret": "..."}}`` on success; we tolerate raw-string
        payloads as well in case a build returns ``{"data": "..."}``.

        **HTTP verb:** newer MikoPBX builds reject ``GET /sip:getSipSecret`` with
        *405 Method not allowed* and require ``POST`` with a JSON body; older
        installs only allow ``GET``. We try **POST first**, then fall back to
        **GET** on 405 / "not allowed with HTTP …", mirroring
        :meth:`force_sip_status_check`.

        Empty string when the username is unknown or the server returns no
        secret. Any non-2xx response (after both attempts) raises
        :class:`MikoRestError`.
        """
        username = (username or "").strip()
        if not username:
            return ""

        def _parse(payload: Any) -> str:
            data = (payload or {}).get("data")
            if isinstance(data, dict):
                secret = (
                    data.get("secret")
                    or data.get("password")
                    or data.get("sip_secret")
                    or data.get("sipSecret")
                    or ""
                )
                if not secret and isinstance(data.get("sipPeer"), dict):
                    sp = data["sipPeer"]
                    secret = (
                        sp.get("secret")
                        or sp.get("password")
                        or sp.get("sip_secret")
                        or ""
                    )
                return str(secret).strip()
            if isinstance(data, str):
                return data.strip()
            return ""

        # MikoPBX v3: GET is often disabled (405). Body shape differs by build:
        # try ``peer`` first (same as getSipPeer in docs), then ``username``.
        post_bodies: tuple[dict[str, str], ...] = (
            {"peer": username},
            {"username": username},
        )

        last_error: MikoRestError | None = None

        for json_body in post_bodies:
            try:
                payload = await self._request(
                    "POST",
                    "/sip:getSipSecret",
                    json_body=json_body,
                )
                secret = _parse(payload)
                if secret:
                    return secret
                # 200 with empty secret — try alternate body key
            except MikoRestError as exc:
                msg = (exc.message or "").lower()
                if exc.status in (400, 404, 422):
                    last_error = exc
                    continue
                if exc.status == 405 or "not allowed with http" in msg:
                    last_error = exc
                    break
                raise

        try:
            payload = await self._request(
                "GET",
                "/sip:getSipSecret",
                params={"username": username},
            )
            return _parse(payload)
        except MikoRestError:
            if last_error is not None:
                raise last_error
            raise

    # ---- realtime ----

    async def get_active_calls(self) -> list[dict]:
        payload = await self._request("GET", "/pbx-status:getActiveCalls")
        return _coerce_row_list((payload or {}).get("data"))

    async def get_active_channels(self) -> list[dict]:
        """Active Asterisk channels — codec, RTP stats, bridge partner.

        Complements :meth:`get_active_calls`: that one is a high-level "call"
        view (call-id, src, dst); this endpoint is the low-level channel view
        with codec/jitter/rtt the softphone can surface in live call details.
        """
        payload = await self._request("GET", "/pbx-status:getActiveChannels")
        return _coerce_row_list((payload or {}).get("data"))


def _coerce_row_list(data: Any) -> list[dict]:
    """Normalise MikoPBX ``data`` field to a list of row dicts.

    Different MikoPBX endpoints return either a plain list (``[...]``) or an
    envelope like ``{"rows": [...], "total": N}``. Iterating a dict would yield
    its keys as strings and blow up downstream with ``'str' object has no
    attribute 'get'`` — this helper shields callers from that footgun.
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("rows", "items", "records", "cdr", "list"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
    return []


def _first_error_message(payload: Any) -> str:
    """Best-effort extractor for the first human-readable error in a response."""
    if not isinstance(payload, dict):
        return str(payload)[:300] if payload else ""
    messages = payload.get("messages") or {}
    if isinstance(messages, dict):
        errs = messages.get("error")
        if isinstance(errs, list) and errs:
            return str(errs[0])
    if isinstance(payload.get("message"), str):
        return payload["message"]
    return ""
