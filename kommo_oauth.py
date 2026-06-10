"""Kommo / AmoCRM OAuth 2.0 helpers for the PBX gateway.

Mirrors the Windows softphone (``AmoCrmOAuthService`` + ``AmoCrmAccountUrl``):
- Authorize via ``https://www.amocrm.ru/oauth``
- Token exchange/refresh only at ``https://{subdomain}.amocrm.ru/oauth2/access_token``
- API base for local OAuth: ``https://{subdomain}.amocrm.ru`` (referer is NOT used for API)
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shutil
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

# Bump when Kommo auth logic changes — shown in admin Test token for deploy verification.
KOMMO_IMPL_VERSION = "20260528-kommo-extension-user-map-v18"

_OAUTH_SUBDOMAIN_URLS = (
    "https://www.amocrm.ru/oauth2/account/subdomain",
    "https://www.amocrm.com/oauth2/account/subdomain",
)


def extract_subdomain_from_referer(referer: str | None) -> str:
    """Same as softphone SettingsWindow OAuth: first label of referer host."""
    if not referer:
        return ""
    domain = referer.replace("https://", "").replace("http://", "").strip().strip("/")
    if not domain:
        return ""
    if "." in domain:
        return domain.split(".")[0]
    return domain


def normalize_subdomain(subdomain: str) -> str:
    s = (subdomain or "").strip().lower()
    for suffix in (".amocrm.ru", ".kommo.com", ".amocrm.com"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip(".")


def normalize_domain_field(raw: str) -> str:
    """Admin Domain field: short name or full host (``mdkb.amocrm.ru``, ``mdkb.kommo.com``)."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s.startswith("https://"):
        s = s[len("https://") :]
    elif s.startswith("http://"):
        s = s[len("http://") :]
    return s.strip().strip("/").split("/")[0].split("?")[0]


def normalize_redirect_uri(uri: str) -> str:
    u = (uri or "").strip()
    while "/tool/tool/" in u:
        u = u.replace("/tool/tool/", "/tool/")
    return u.rstrip("/")


def looks_like_kommo_authorization_code(token: str) -> bool:
    """OAuth authorization codes (Keys tab «Код авторизации») are not API bearer tokens."""
    t = (token or "").strip()
    if not t or t.count(".") >= 2:
        return False  # JWT access tokens contain dots
    return t.startswith("def5020") and len(t) > 80


def sanitize_access_token(raw: str) -> str:
    """Remove accidental whitespace, ``Bearer `` prefix, or quotes from pasted tokens."""
    t = (raw or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return "".join(t.split())


def reject_if_authorization_code(token: str) -> None:
    if looks_like_kommo_authorization_code(token):
        raise ValueError(
            "This looks like Kommo «Код авторизации» (authorization code), not an API token. "
            "Use «Сгенерировать токен» on the Keys tab, or complete OAuth via Authorize with Kommo."
        )


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return "https://www.amocrm.ru/oauth?" + urllib.parse.urlencode(params)


def account_base_url_from_referer(referer: str | None) -> str:
    """``AmoCrmAccountUrl.TryBuildWebBaseUrlFromReferer``."""
    if not referer:
        return ""
    s = referer.strip()
    if s.startswith("https://"):
        s = s[len("https://") :]
    elif s.startswith("http://"):
        s = s[len("http://") :]
    s = s.strip().strip("/")
    host = s.split("/")[0].split("?")[0]
    return f"https://{host}" if host else ""


def build_api_web_base_url(subdomain_raw: str | None, referer: str | None = None) -> str:
    """``AmoCrmAccountUrl.TryBuildWebBaseUrl`` — short subdomain → ``.amocrm.ru``."""
    if not subdomain_raw or not str(subdomain_raw).strip():
        return ""

    s = str(subdomain_raw).strip()
    if "." in s:
        domain = s
        if domain.startswith("https://"):
            domain = domain[len("https://") :]
        elif domain.startswith("http://"):
            domain = domain[len("http://") :]
        domain = domain.strip().strip("/")
        return f"https://{domain}" if domain else ""

    sub = normalize_subdomain(s)
    return f"https://{sub}.amocrm.ru" if sub else ""


def build_softphone_api_web_base_url(subdomain_raw: str | None) -> str:
    """Local OAuth in Callspire: API host from subdomain only (``InitializeOAuthAsync``)."""
    return build_api_web_base_url(subdomain_raw, referer=None)


def build_gateway_api_web_base_url(subdomain_raw: str | None, referer: str | None = None) -> str:
    """Gateway session / ``InitializeGatewayOAuthAsync``: prefer OAuth referer host."""
    from_referer = account_base_url_from_referer(referer)
    if from_referer:
        return from_referer
    return build_api_web_base_url(subdomain_raw, referer=None)


def token_endpoint_url(domain: str) -> str:
    """Token exchange/refresh host from admin Domain or OAuth referer."""
    host = normalize_domain_field(domain)
    if not host:
        raise ValueError("Kommo domain is required for token exchange")
    if "." in host:
        return f"https://{host}/oauth2/access_token"
    return f"https://{host}.amocrm.ru/oauth2/access_token"


def decode_jwt_payload(token: str) -> dict:
    """Decode AmoCRM JWT access_token payload (no signature verification)."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def jwt_debug_info(access: str) -> dict:
    """Non-secret JWT claims for admin troubleshooting."""
    payload = decode_jwt_payload(sanitize_access_token(access))
    if not payload:
        return {}

    integration_id = (
        payload.get("client_uuid")
        or payload.get("client_id")
        or payload.get("integration_id")
        or payload.get("application_id")
    )
    aud = payload.get("aud")
    if not integration_id and aud:
        if isinstance(aud, list) and aud:
            integration_id = aud[0]
        elif isinstance(aud, str):
            integration_id = aud

    scopes_raw = payload.get("scopes")
    if scopes_raw is None:
        scopes_raw = payload.get("scope")

    scopes_list: list[str] = []
    if isinstance(scopes_raw, (list, tuple, set)):
        scopes_list = [str(s) for s in scopes_raw if str(s).strip()]
    elif isinstance(scopes_raw, dict):
        for key, val in scopes_raw.items():
            key_s = str(key).strip()
            if not key_s:
                continue
            if val in (True, 1, "1", "true", "yes"):
                scopes_list.append(key_s)
            elif val not in (False, 0, "0", "false", "no", None, ""):
                scopes_list.append(f"{key_s}={val}")
    elif isinstance(scopes_raw, str) and scopes_raw.strip():
        scopes_list = [s.strip() for s in scopes_raw.replace(",", " ").split() if s.strip()]

    permissions = payload.get("permissions") or payload.get("actions") or scopes_list
    if permissions is None and isinstance(payload.get("access"), (list, dict)):
        permissions = payload.get("access")

    return {
        "account_id": payload.get("account_id"),
        "subdomain": payload.get("subdomain"),
        "api_domain": payload.get("api_domain") or payload.get("apiDomain"),
        "user_id": payload.get("user_id") or payload.get("sub"),
        "client_uuid": integration_id,
        "permissions": permissions,
        "scopes": scopes_list,
        "exp": payload.get("exp"),
        "payload_keys": sorted(str(k) for k in payload.keys()),
    }


def jwt_crm_api_likely_enabled(access: str) -> bool | None:
    """True when JWT ``scopes`` includes Kommo CRM access (typically ``crm``)."""
    info = jwt_debug_info(access)
    scopes = info.get("scopes")
    if isinstance(scopes, list):
        if not scopes:
            return False
        normalized = {str(s).strip().lower() for s in scopes if str(s).strip()}
        if "crm" in normalized:
            return True
        # Legacy / custom scope strings
        flat = " ".join(normalized)
        hints = ("crm", "api", "account", "lead", "contact", "full", "all")
        return any(h in flat for h in hints)

    perms = info.get("permissions")
    if perms is None:
        return None
    if isinstance(perms, dict):
        perms = list(perms.keys()) + list(perms.values())
    if isinstance(perms, str):
        perms = [perms]
    if not isinstance(perms, (list, tuple, set)):
        return None
    flat = " ".join(str(p).lower() for p in perms)
    if not flat.strip():
        return False
    hints = ("crm", "api", "account", "lead", "contact", "full", "all")
    return any(h in flat for h in hints)


def api_host_from_jwt(access: str) -> str:
    """``api_domain`` claim host for Amo OAuth shard token endpoints — not ``/api/v4`` account API."""
    payload = decode_jwt_payload(sanitize_access_token(access))
    api_domain = str(payload.get("api_domain") or payload.get("apiDomain") or "").strip()
    if not api_domain:
        return ""
    if "." in api_domain:
        return api_domain.split("/")[0].split("?")[0]
    tld = str(payload.get("top_level_domain") or payload.get("account_tld") or "ru").strip() or "ru"
    suffix = "kommo.com" if tld == "com" else "amocrm.ru"
    return f"{api_domain}.{suffix}"


def account_base_from_domain_info(info: dict | None) -> str:
    if not info:
        return ""
    domain = str(info.get("domain") or "").strip()
    if domain:
        host = domain.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0]
        return f"https://{host}" if host else ""
    sub = normalize_subdomain(str(info.get("subdomain") or ""))
    if not sub:
        return ""
    tld = str(info.get("top_level_domain") or "ru").strip() or "ru"
    suffix = "kommo.com" if tld == "com" else "amocrm.ru"
    return f"https://{sub}.{suffix}"


def oauth_api_probe_bases(domain: str) -> list[str]:
    """Candidate API hosts from admin Domain (RU + Kommo global TLD)."""
    host = normalize_domain_field(domain)
    if not host:
        return []
    ordered: list[str] = []
    primary = build_api_web_base_url(host)
    if primary:
        ordered.append(primary)
    if "." not in host:
        for suffix in ("amocrm.ru", "kommo.com"):
            candidate = f"https://{host}.{suffix}"
            if candidate not in ordered:
                ordered.append(candidate)
    return ordered


def kommo_api_headers(access: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {sanitize_access_token(access)}",
        "User-Agent": "Callspire-Softphone/1.0",
        "Accept": "application/json, application/hal+json",
    }


_kommo_api_headers = kommo_api_headers


def _looks_like_account_json(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(k in payload for k in ("id", "name", "subdomain", "current_user_id"))


def _parse_probe_json_body(body: str) -> tuple[dict | None, str]:
    try:
        payload = json.loads(body)
    except Exception as exc:
        return None, f"invalid JSON: {exc}"
    if not _looks_like_account_json(payload):
        return None, "JSON response is not Kommo account payload"
    return payload, ""


async def probe_kommo_account_api(
    account_base: str,
    access: str,
    *,
    timeout: float = 20.0,
) -> dict:
    """Probe ``GET /api/v4/account`` — curl first (matches server manual tests), httpx fallback."""
    token = sanitize_access_token(access or "")
    base = (account_base or "").strip().rstrip("/")
    result: dict = {
        "account_base_url": base,
        "ok": False,
        "status_code": None,
        "content_type": "",
        "is_json": False,
        "account_id": None,
        "body_preview": "",
        "error": "",
        "transport": "",
    }
    if not token or not base:
        result["error"] = "missing token or account base"
        return result

    url = f"{base}/api/v4/account?with=drive_url"

    curl_result = await _probe_kommo_account_api_curl(url, token, timeout=timeout)
    curl_result["account_base_url"] = base
    if curl_result.get("ok"):
        return curl_result

    httpx_result = await _probe_kommo_account_api_httpx(url, token, timeout=timeout)
    httpx_result["account_base_url"] = base
    if httpx_result.get("ok"):
        if curl_result.get("status_code") and curl_result.get("status_code") != 200:
            print(
                f"[kommo] probe curl HTTP {curl_result.get('status_code')} but httpx OK for {base}",
                flush=True,
            )
        return httpx_result

    result.update(httpx_result or curl_result)
    result["error"] = (
        f"curl: {curl_result.get('error') or '?'}; "
        f"httpx: {httpx_result.get('error') or '?'}"
    )
    result["status_code"] = httpx_result.get("status_code") or curl_result.get("status_code")
    result["body_preview"] = httpx_result.get("body_preview") or curl_result.get("body_preview") or ""
    result["transport"] = "curl+httpx"
    curl_code = curl_result.get("status_code")
    httpx_code = httpx_result.get("status_code")
    if curl_code != httpx_code:
        print(
            f"[kommo] probe transport mismatch for {base}: curl={curl_code} httpx={httpx_code}",
            flush=True,
        )
    return result


async def _probe_kommo_account_api_curl(url: str, token: str, *, timeout: float) -> dict:
    result: dict = {
        "ok": False,
        "status_code": None,
        "content_type": "",
        "is_json": False,
        "account_id": None,
        "body_preview": "",
        "error": "",
        "transport": "curl",
    }
    if not shutil.which("curl"):
        result["error"] = "curl not installed"
        return result

    marker = "__KOMMO_HTTP__:"
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(max(1, int(timeout))),
        "-w",
        f"\n{marker}%{{http_code}}",
        "-H",
        f"Authorization: Bearer {token}",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5.0)
    except Exception as exc:
        result["error"] = f"curl exec: {exc}"
        return result

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        result["error"] = f"curl exit {proc.returncode}: {err[:200]}"
        return result

    text = (stdout or b"").decode("utf-8", errors="replace")
    if marker not in text:
        result["error"] = "curl response missing HTTP marker"
        result["body_preview"] = text[:240]
        return result

    body, _, code_part = text.rpartition(marker)
    body = body.rstrip("\n")
    try:
        status_code = int(code_part.strip())
    except ValueError:
        result["error"] = f"curl bad HTTP code: {code_part!r}"
        return result

    result["status_code"] = status_code
    result["body_preview"] = body[:240]
    if status_code != 200:
        result["error"] = f"HTTP {status_code}"
        return result

    payload, err = _parse_probe_json_body(body)
    if err:
        result["error"] = err
        return result

    result["is_json"] = True
    result["account_id"] = payload.get("id")
    result["current_user_id"] = payload.get("current_user_id")
    result["ok"] = True
    return result


async def _probe_kommo_account_api_httpx(url: str, token: str, *, timeout: float) -> dict:
    result: dict = {
        "ok": False,
        "status_code": None,
        "content_type": "",
        "is_json": False,
        "account_id": None,
        "body_preview": "",
        "error": "",
        "transport": "httpx",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, http2=False) as client:
            resp = await client.get(url, headers=kommo_api_headers(token))
            result["status_code"] = resp.status_code
            result["content_type"] = resp.headers.get("content-type") or ""
            result["body_preview"] = (resp.text or "")[:240]
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location") or ""
                result["error"] = f"HTTP {resp.status_code} redirect to {loc or '?'}"
                return result
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            payload, err = _parse_probe_json_body(resp.text or "")
            if err:
                result["error"] = err
                return result
            result["is_json"] = True
            result["account_id"] = payload.get("id")
            result["current_user_id"] = payload.get("current_user_id")
            result["ok"] = True
            return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def _content_type_looks_json(content_type: str) -> bool:
    ct = (content_type or "").lower().split(";")[0].strip()
    return ct.endswith("+json") or ct == "application/json"


async def fetch_account_domain_info(
    access: str,
    *,
    refresh: str = "",
    timeout: float = 20.0,
) -> dict | None:
    """Resolve Kommo account domain via official OAuth endpoints (AmoCRM docs)."""
    token = sanitize_access_token(access)
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Callspire-Softphone/1.0",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for url in _OAUTH_SUBDOMAIN_URLS:
            try:
                resp = await client.get(url, headers=headers)
                ctype = (resp.headers.get("content-type") or "").lower()
                print(
                    f"[kommo] GET {url} -> HTTP {resp.status_code} ctype={ctype[:40]}",
                    flush=True,
                )
                if resp.status_code == 200 and _content_type_looks_json(ctype):
                    data = resp.json()
                    if isinstance(data, dict) and (data.get("domain") or data.get("subdomain")):
                        return data
            except Exception as exc:
                print(f"[kommo] account/subdomain {url} error: {exc}", flush=True)

    refresh = (refresh or "").strip()
    api_host = api_host_from_jwt(token)
    if refresh and api_host:
        url = f"https://{api_host}/oauth2/account/current/subdomain"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.get(
                    url,
                    headers={
                        "X-Refresh-Token": refresh,
                        "User-Agent": "Callspire-Softphone/1.0",
                        "Accept": "application/json",
                    },
                )
                print(
                    f"[kommo] GET {url} (refresh) -> HTTP {resp.status_code}",
                    flush=True,
                )
                if resp.status_code == 200 and _content_type_looks_json(
                    resp.headers.get("content-type") or ""
                ):
                    data = resp.json()
                    if isinstance(data, dict) and (data.get("domain") or data.get("subdomain")):
                        return data
        except Exception as exc:
            print(f"[kommo] account/current/subdomain error: {exc}", flush=True)
    return None


def _format_kommo_token_error(status_code: int, body: str, *, redirect_uri: str = "") -> str:
    """Turn Kommo token-endpoint errors into actionable admin messages."""
    detail = ""
    hint = ""
    try:
        payload = json.loads(body) if body.strip().startswith("{") else None
        if isinstance(payload, dict):
            parts = [
                str(payload.get("title") or "").strip(),
                str(payload.get("detail") or payload.get("hint") or "").strip(),
                str(payload.get("type") or "").strip(),
            ]
            detail = " — ".join(p for p in parts if p)
            err_type = str(payload.get("type") or "").lower()
            if "redirect" in err_type or "redirect" in detail.lower():
                hint = (
                    f" redirect_uri sent: {redirect_uri!r}. "
                    "It must match Kommo integration settings and the Authorize redirect exactly."
                )
            elif "code" in err_type or "authorization" in detail.lower():
                hint = (
                    " Authorization codes are single-use and expire in ~20 minutes. "
                    "Do not refresh this callback page — click Authorize with Kommo again in admin."
                )
            elif "secret" in err_type or "client" in detail.lower():
                hint = " Regenerate Client Secret in Kommo, paste it in admin, Save settings, then Authorize again."
    except Exception:
        pass

    body_preview = (body or "").strip().replace("\n", " ")[:320]
    msg = f"Kommo token endpoint HTTP {status_code}"
    if detail:
        msg += f": {detail}"
    elif body_preview:
        msg += f": {body_preview}"
    if hint:
        msg += hint
    return msg


async def _post_token_request(
    url: str,
    data: dict,
    *,
    timeout: float,
    redirect_uri: str = "",
) -> dict:
    grant = data.get("grant_type") or "?"
    client_id = str(data.get("client_id") or "")
    print(
        f"[kommo] token POST grant={grant} client_id={client_id[:8]}... "
        f"redirect_uri={redirect_uri!r} url={url}",
        flush=True,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Match Windows softphone (AmoCrmOAuthService): form-urlencoded first, JSON fallback only.
        last_status = 0
        last_body = ""
        for attempt, send in enumerate(("form", "json"), start=1):
            if send == "form":
                resp = await client.post(
                    url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                resp = await client.post(url, json=data)
            if resp.status_code < 400:
                payload = resp.json()
                access = payload.get("access_token") or ""
                print(
                    f"[kommo] token exchange OK via {send} access_len={len(str(access))} "
                    f"jwt={str(access).startswith('eyJ')}",
                    flush=True,
                )
                payload["_exchange_transport"] = send
                return payload
            last_status = resp.status_code
            last_body = resp.text or ""
            print(
                f"[kommo] token {send} POST HTTP {resp.status_code} for {url}: "
                f"{last_body[:500]}",
                flush=True,
            )

    raise ValueError(_format_kommo_token_error(last_status, last_body, redirect_uri=redirect_uri))


async def exchange_code_for_tokens(
    *,
    subdomain: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    referer: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict, str]:
    sub = normalize_subdomain(subdomain) or extract_subdomain_from_referer(referer)
    domain = normalize_domain_field(subdomain) or normalize_domain_field(referer) or sub
    url = token_endpoint_url(domain)
    redirect = normalize_redirect_uri(redirect_uri)
    if not redirect:
        raise ValueError("redirect_uri is required for Kommo authorization code exchange")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": (code or "").strip(),
        "redirect_uri": redirect,
    }
    payload = await _post_token_request(
        url, data, timeout=timeout, redirect_uri=redirect
    )
    return payload, url


async def refresh_access_token(
    *,
    subdomain: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    redirect_uri: str,
    referer: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Amo docs: refresh grant requires ``redirect_uri`` matching integration settings."""
    sub = normalize_subdomain(subdomain) or extract_subdomain_from_referer(referer)
    domain = normalize_domain_field(subdomain) or normalize_domain_field(referer) or sub
    url = token_endpoint_url(domain)
    redirect = normalize_redirect_uri(redirect_uri)
    if not redirect:
        raise ValueError("redirect_uri is required for Kommo token refresh (AmoCRM OAuth docs)")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": redirect,
    }
    return await _post_token_request(url, data, timeout=timeout, redirect_uri=redirect)


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def expires_at_from_seconds(expires_in: int | None) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


async def fetch_kommo_users(
    account_base: str,
    access: str,
    *,
    timeout: float = 30.0,
    max_pages: int = 20,
) -> list[dict]:
    """List active Kommo users for extension → user mapping in admin UI."""
    token = sanitize_access_token(access or "")
    base = (account_base or "").strip().rstrip("/")
    if not token or not base:
        return []

    users: list[dict] = []
    page = 1
    url = f"{base}/api/v4/users"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, http2=False) as client:
        while page <= max_pages:
            resp = await client.get(
                url,
                params={"limit": 250, "page": page, "with": "role"},
                headers=kommo_api_headers(token),
            )
            if resp.status_code != 200:
                raise ValueError(f"Kommo users API HTTP {resp.status_code}: {(resp.text or '')[:200]}")
            try:
                payload = resp.json()
            except Exception as exc:
                raise ValueError(f"Kommo users API invalid JSON: {exc}") from exc

            embedded = payload.get("_embedded") if isinstance(payload, dict) else None
            batch = embedded.get("users") if isinstance(embedded, dict) else None
            if not isinstance(batch, list) or not batch:
                break

            for item in batch:
                if not isinstance(item, dict):
                    continue
                uid = item.get("id")
                if uid is None:
                    continue
                users.append(
                    {
                        "id": int(uid),
                        "name": (item.get("name") or "").strip(),
                        "email": (item.get("email") or "").strip(),
                        "is_active": bool(item.get("is_active", True)),
                    }
                )

            if len(batch) < 250:
                break
            page += 1

    users.sort(key=lambda u: ((u.get("name") or u.get("email") or str(u.get("id"))).lower()))
    return users
