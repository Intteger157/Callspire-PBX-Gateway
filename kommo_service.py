"""Kommo integration business logic for the PBX gateway."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import permissions_db
from kommo_oauth import (
    KOMMO_IMPL_VERSION,
    account_base_from_domain_info,
    account_base_url_from_referer,
    build_authorize_url,
    build_gateway_api_web_base_url,
    build_softphone_api_web_base_url,
    decode_jwt_payload,
    exchange_code_for_tokens,
    expires_at_from_seconds,
    fetch_account_domain_info,
    fetch_kommo_users,
    jwt_crm_api_likely_enabled,
    jwt_debug_info,
    normalize_domain_field,
    normalize_redirect_uri,
    normalize_subdomain,
    oauth_api_probe_bases,
    probe_kommo_account_api,
    refresh_access_token,
    sanitize_access_token,
    token_endpoint_url,
)
from secret_vault import decrypt_secret, encrypt_secret

_kommo_ops_lock = asyncio.Lock()


def _parse_expires_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _token_needs_refresh(expires_at: str | None, *, skew_seconds: int = 300) -> bool:
    dt = _parse_expires_at(expires_at)
    if dt is None:
        return True
    return datetime.now(timezone.utc).timestamp() >= (dt.timestamp() - skew_seconds)


def get_admin_config(*, jwt_secret: str, default_redirect_uri: str) -> dict:
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    secret_enc = cfg.get("client_secret") or ""
    default_redirect_uri = normalize_redirect_uri(default_redirect_uri)
    saved = normalize_redirect_uri((cfg.get("redirect_uri") or "").strip())
    redirect = saved or default_redirect_uri
    sub = cfg.get("subdomain") or ""
    domain = normalize_domain_field(sub) or sub
    account_base = (tokens.get("account_base_url") or "").strip().rstrip("/")
    if not account_base and domain:
        account_base = build_softphone_api_web_base_url(domain) or ""
    return {
        "enabled": bool(cfg.get("enabled")),
        "client_id": cfg.get("client_id") or "",
        "client_secret": "***" if secret_enc else "",
        "redirect_uri": redirect,
        "default_redirect_uri": default_redirect_uri,
        "redirect_uri_saved": saved,
        "domain": domain,
        "subdomain": domain,
        "account_base_url": account_base,
        "authorized": bool(tokens.get("access_token")),
        "authorized_at": tokens.get("authorized_at"),
        "token_expires_at": tokens.get("expires_at"),
        "excluded_extensions": permissions_db.parse_kommo_excluded_extensions(
            cfg.get("excluded_extensions") or "[]"
        ),
        "extension_users": permissions_db.list_kommo_extension_users(),
    }


async def _fetch_live_kommo_users(*, jwt_secret: str) -> tuple[list[dict], str]:
    """Return (users, account_base) from Kommo API."""
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    if not tokens.get("access_token"):
        raise ValueError("Kommo is not authorized on gateway")

    access = _decrypt_access_token(tokens.get("access_token") or "", jwt_secret)
    if not access:
        raise ValueError("Kommo access token is missing")

    account_base = _account_base_for_cfg(cfg, tokens)
    if not account_base:
        raise ValueError("Kommo account base URL is not configured")

    users = await fetch_kommo_users(account_base, access)
    return users, account_base


def _build_admin_kommo_users_payload(live_users: list[dict]) -> dict:
    """Active Kommo users for dropdown + assigned users missing from Kommo (assignments preserved)."""
    by_id: dict[int, dict] = {}
    for u in live_users:
        try:
            by_id[int(u["id"])] = u
        except (TypeError, ValueError, KeyError):
            continue

    active = [u for u in live_users if u.get("is_active", True)]
    merged: list[dict] = []
    seen: set[int] = set()

    for u in active:
        uid = int(u["id"])
        merged.append({**u, "missing_in_kommo": False})
        seen.add(uid)

    assigned = permissions_db.list_kommo_extension_users()
    orphan_ids: set[int] = set()
    for mapping in assigned.values():
        try:
            uid = int(mapping.get("kommo_user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in by_id:
            orphan_ids.add(uid)

    for uid in sorted(orphan_ids):
        if uid in seen:
            continue
        name = ""
        for mapping in assigned.values():
            if int(mapping.get("kommo_user_id") or 0) == uid:
                name = (mapping.get("kommo_user_name") or "").strip()
                break
        merged.append(
            {
                "id": uid,
                "name": name or f"User {uid}",
                "email": "",
                "is_active": False,
                "missing_in_kommo": True,
            }
        )
        seen.add(uid)

    inactive_live = [
        u for u in live_users if not u.get("is_active", True) and int(u.get("id") or 0) not in seen
    ]
    for u in inactive_live:
        uid = int(u["id"])
        merged.append({**u, "missing_in_kommo": False})
        seen.add(uid)

    merged.sort(key=lambda u: ((u.get("name") or u.get("email") or str(u.get("id"))).lower()))
    inactive_count = sum(1 for u in live_users if not u.get("is_active", True))
    return {
        "users": merged,
        "count": len(merged),
        "total_count": len(live_users),
        "active_count": len(active),
        "inactive_count": inactive_count,
        "orphan_count": len(orphan_ids),
    }


async def list_admin_kommo_users(*, jwt_secret: str) -> dict:
    """Kommo users for PBX extension mapping in admin UI."""
    live_users, _account_base = await _fetch_live_kommo_users(jwt_secret=jwt_secret)
    return _build_admin_kommo_users_payload(live_users)


async def sync_admin_kommo_users(*, jwt_secret: str) -> dict:
    """Refresh Kommo user list from AmoCRM; update stored names but keep all extension mappings."""
    live_users, _account_base = await _fetch_live_kommo_users(jwt_secret=jwt_secret)
    by_id = {int(u["id"]): u for u in live_users if u.get("id") is not None}
    names_updated = permissions_db.refresh_kommo_extension_user_names(by_id)
    payload = _build_admin_kommo_users_payload(live_users)
    payload["names_updated"] = names_updated
    payload["synced_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def save_kommo_extension_user(
    *,
    extension: str,
    kommo_user_id: int | None,
    kommo_user_name: str = "",
) -> None:
    permissions_db.set_kommo_extension_user(extension, kommo_user_id, kommo_user_name)


async def _acting_kommo_user_for_extension(
    *,
    access: str,
    account_base: str,
    extension: str | None,
) -> dict:
    """Resolve Kommo user id for call attribution (extension map or token owner fallback)."""
    ext = (extension or "").strip()
    mapped = permissions_db.get_kommo_extension_user(ext) if ext else None
    if mapped and mapped.get("kommo_user_id"):
        return {
            "kommo_user_id": int(mapped["kommo_user_id"]),
            "kommo_user_name": mapped.get("kommo_user_name") or "",
            "kommo_user_id_source": "extension_map",
        }

    probe = await probe_kommo_account_api(account_base, access)
    token_owner = probe.get("current_user_id") if probe.get("ok") else None
    if token_owner:
        return {
            "kommo_user_id": int(token_owner),
            "kommo_user_name": "",
            "kommo_user_id_source": "token_owner",
        }

    return {
        "kommo_user_id": None,
        "kommo_user_name": "",
        "kommo_user_id_source": "unknown",
    }


def save_kommo_excluded_extensions(excluded_extensions: list[str]) -> None:
    permissions_db.set_kommo_excluded_extensions(excluded_extensions)


def save_admin_config(
    *,
    jwt_secret: str,
    enabled: bool,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    subdomain: str | None = None,
) -> None:
    plain_secret: str | None = None
    if client_secret is not None and client_secret.strip() and client_secret.strip() != "***":
        plain_secret = encrypt_secret(client_secret.strip(), jwt_secret)
    elif client_secret is None or not str(client_secret or "").strip():
        current = permissions_db.get_kommo_integration()
        plain_secret = current.get("client_secret") or ""
    else:
        current = permissions_db.get_kommo_integration()
        plain_secret = current.get("client_secret") or ""

    sub_value: str | None = None
    if subdomain is not None:
        sub_value = normalize_domain_field(subdomain) or subdomain.strip()

    permissions_db.set_kommo_integration(
        enabled=enabled,
        client_id=client_id.strip(),
        client_secret=plain_secret,
        redirect_uri=normalize_redirect_uri(redirect_uri.strip()),
        subdomain=sub_value,
    )


def _apply_extension_exclusion(status: dict, extension: str | None) -> dict:
    out = dict(status)
    ext = (extension or "").strip()
    excluded = bool(ext) and permissions_db.is_kommo_extension_excluded(ext)
    out["excluded"] = excluded
    out["offer_gateway"] = bool(out.get("enabled")) and not excluded
    if excluded:
        out["available"] = False
        out["token_valid"] = False
        out["needs_reauthorize"] = False
        out.pop("error", None)
    return out


async def kommo_provision_hints_for_extension(*, jwt_secret: str, extension: str) -> dict:
    """Desktop auto-setup after admin provisioning link — shared Kommo when mapped."""
    import kommo_store

    ext = (extension or "").strip()
    status = await get_client_status_async(jwt_secret=jwt_secret, extension=ext or None)
    mapped = kommo_store.get_extension_mapping(ext) if ext else {}
    kommo_user_id = mapped.get("kommo_user_id")
    kommo_user_name = (mapped.get("kommo_user_name") or "").strip()
    excluded = bool(status.get("excluded"))
    offer = bool(status.get("offer_gateway"))
    # Mapped Kommo user in admin (and not excluded) — desktop enables gateway mode even
    # if OAuth session is not ready yet (init retries after winapp-auth JWT).
    upload_enabled = bool(kommo_user_id) and not excluded
    authorized = bool(status.get("authorized"))
    return {
        "configure_gateway": upload_enabled,
        "offer_gateway": offer,
        "upload_enabled": upload_enabled,
        "excluded": excluded,
        "authorized": authorized,
        "kommo_user_id": kommo_user_id,
        "kommo_user_name": kommo_user_name,
        "subdomain": status.get("subdomain") or "",
    }


def _decrypt_client_secret(enc: str, jwt_secret: str) -> str:
    if not enc:
        return ""
    try:
        return decrypt_secret(enc, jwt_secret)
    except Exception as exc:
        print(f"[kommo] client_secret decrypt failed: {exc}", flush=True)
        return ""


def _decrypt_access_token(enc: str, jwt_secret: str) -> str:
    if not enc:
        return ""
    return sanitize_access_token(decrypt_secret(enc, jwt_secret))


def _account_base_for_cfg(cfg: dict, tokens: dict) -> str:
    stored = (tokens.get("account_base_url") or "").strip().rstrip("/")
    if stored:
        return stored
    sub = cfg.get("subdomain") or ""
    return build_gateway_api_web_base_url(sub, tokens.get("referer") or None)


def _account_base_candidates(
    cfg: dict,
    tokens: dict,
    *,
    prefer: str | None = None,
    domain_info: dict | None = None,
    access: str = "",
) -> list[str]:
    """Probe order: OAuth domain API, referer, admin domain — never OAuth shard (api-b.*)."""
    sub = cfg.get("subdomain") or ""
    softphone_base = build_softphone_api_web_base_url(sub)
    referer_base = account_base_url_from_referer(tokens.get("referer") or "")
    resolved_base = account_base_from_domain_info(domain_info)
    stored = (tokens.get("account_base_url") or "").strip().rstrip("/")
    prefer = (prefer or "").strip().rstrip("/")

    ordered: list[str] = []
    for candidate in (
        softphone_base,
        resolved_base,
        referer_base,
        prefer,
        *oauth_api_probe_bases(sub),
        stored,
    ):
        c = (candidate or "").strip().rstrip("/")
        if c and c not in ordered:
            ordered.append(c)
    return ordered


_INTEGRATION_API_HINT = (
    "In Kommo → Settings → Integrations → your gateway app: open «Allow access» / "
    "«Предоставить доступ» and enable CRM API (contacts, leads, account data). "
    "If the integration was disabled by an admin, re-install it and Authorize again. "
    "If the Windows softphone already works with another integration (fca4e256-…), use that "
    "Client ID + Secret on this gateway and add this Redirect URI to that integration instead."
)


def _kommo_token_diagnosis(access: str, cfg: dict) -> dict:
    jwt_info = jwt_debug_info(access)
    client_match = _client_id_matches_jwt(cfg, access)
    crm_hint = jwt_crm_api_likely_enabled(access)
    return {
        "jwt": jwt_info,
        "client_id_matches_jwt": client_match,
        "crm_api_in_jwt": crm_hint,
    }


def _account_api_unavailable_message(
    *,
    probe_details: list[dict] | str,
    jwt_info: dict | None = None,
    diagnosis: dict | None = None,
) -> str:
    detail = probe_details if isinstance(probe_details, str) else _format_probe_details(probe_details)
    extra = ""
    if jwt_info:
        client_uuid = jwt_info.get("client_uuid")
        account_id = jwt_info.get("account_id")
        if client_uuid:
            extra += f" JWT integration={client_uuid}."
        if account_id:
            extra += f" account_id={account_id}."
        scopes = jwt_info.get("scopes")
        if isinstance(scopes, list):
            if not scopes:
                extra += " JWT scopes=[] (no CRM access — enable «crm» scope in Kommo integration)."
            else:
                extra += f" JWT scopes={scopes!r}."
    if diagnosis:
        crm = diagnosis.get("crm_api_in_jwt")
        if crm is False:
            extra += " JWT has no CRM/API permission claims — enable scopes in Kommo integration settings."
        match = diagnosis.get("client_id_matches_jwt")
        if match is False:
            extra += " Token is from a different Kommo integration than configured Client ID."
    return f"Kommo access token is not valid for CRM API.{extra} Probe: {detail}. {_INTEGRATION_API_HINT}"


def _resolve_account_base_url(
    cfg: dict,
    tokens: dict,
    *,
    domain_info: dict | None = None,
    prefer: str | None = None,
) -> str:
    sub = cfg.get("subdomain") or ""
    for candidate in (
        account_base_from_domain_info(domain_info),
        account_base_url_from_referer(tokens.get("referer") or ""),
        (prefer or "").strip().rstrip("/"),
        build_softphone_api_web_base_url(sub),
        (tokens.get("account_base_url") or "").strip().rstrip("/"),
    ):
        c = (candidate or "").strip().rstrip("/")
        if c:
            return c
    return ""


async def _confirm_oauth_token(access: str, refresh: str = "") -> tuple[bool, dict | None]:
    domain_info = await fetch_account_domain_info(access, refresh=refresh)
    ok = bool(domain_info and (domain_info.get("domain") or domain_info.get("subdomain")))
    return ok, domain_info


def _client_id_matches_jwt(cfg: dict, access: str) -> bool | None:
    expected = (cfg.get("client_id") or "").strip().lower()
    jwt_client = str(jwt_debug_info(access).get("client_uuid") or "").strip().lower()
    if not expected or not jwt_client:
        return None
    return jwt_client == expected


def start_oauth(*, jwt_secret: str, default_redirect_uri: str) -> dict:
    cfg = permissions_db.get_kommo_integration()
    client_id = (cfg.get("client_id") or "").strip()
    secret_enc = cfg.get("client_secret") or ""
    redirect_uri = normalize_redirect_uri(
        (cfg.get("redirect_uri") or "").strip() or default_redirect_uri
    )
    if not client_id or not secret_enc:
        raise ValueError(
            "Save Client ID and Client Secret first (click Save settings), then Authorize."
        )
    if not redirect_uri:
        raise ValueError("Redirect URI is required")

    print(f"[kommo] OAuth start client_id={client_id[:8]}... redirect_uri={redirect_uri!r}", flush=True)

    state = permissions_db.create_kommo_oauth_state(redirect_uri=redirect_uri)
    auth_url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
    )
    return {"auth_url": auth_url, "state": state, "redirect_uri": redirect_uri}


async def complete_oauth_callback(
    *,
    code: str,
    referer: str,
    state: str,
    jwt_secret: str,
    default_redirect_uri: str,
) -> dict:
    async with _kommo_ops_lock:
        return await _complete_oauth_callback_impl(
            code=code,
            referer=referer,
            state=state,
            jwt_secret=jwt_secret,
            default_redirect_uri=default_redirect_uri,
        )


async def _complete_oauth_callback_impl(
    *,
    code: str,
    referer: str,
    state: str,
    jwt_secret: str,
    default_redirect_uri: str,
) -> dict:
    oauth_state = permissions_db.consume_kommo_oauth_state(state)
    if oauth_state is None:
        raise ValueError(
            "Invalid or expired OAuth state. Do not refresh this page — "
            "return to admin and click Authorize with Kommo again."
        )

    referer = (referer or "").strip()
    if not referer:
        raise ValueError(
            "Kommo did not return referer in OAuth callback — cannot determine account subdomain. "
            "Try Authorize again."
        )

    cfg = permissions_db.get_kommo_integration()
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = _decrypt_client_secret(cfg.get("client_secret") or "", jwt_secret)
    redirect_from_state = normalize_redirect_uri(oauth_state.get("redirect_uri") or "")
    redirect_from_cfg = normalize_redirect_uri(
        (cfg.get("redirect_uri") or "").strip() or default_redirect_uri
    )
    redirect_uri = redirect_from_state or redirect_from_cfg
    domain = normalize_domain_field(referer)
    if not domain:
        raise ValueError("Could not determine Kommo domain from OAuth referer")
    if not code:
        raise ValueError("Missing authorization code")
    if not client_secret:
        raise ValueError(
            "Client Secret is missing or could not be decrypted. "
            "Paste the secret from Kommo integration settings, Save settings, then Authorize again."
        )

    print(
        f"[kommo] OAuth callback domain={domain} referer={referer!r} "
        f"redirect_uri={redirect_uri!r} client_id={client_id[:8]}... "
        f"token_url={token_endpoint_url(domain)}",
        flush=True,
    )

    payload, token_url = await exchange_code_for_tokens(
        subdomain=domain,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        referer=referer,
    )
    exchange_via = payload.pop("_exchange_transport", "?")
    access = sanitize_access_token(payload.get("access_token") or "")
    refresh = sanitize_access_token(payload.get("refresh_token") or "")
    if not access:
        raise ValueError("Kommo did not return an access token")
    if not access.startswith("eyJ"):
        print(
            f"[kommo] WARNING: access_token does not look like JWT (len={len(access)} "
            f"prefix={access[:12]!r})",
            flush=True,
        )

    account_base = build_softphone_api_web_base_url(domain)
    expires_at = expires_at_from_seconds(payload.get("expires_in"))
    print(
        f"[kommo] token exchange OK via {exchange_via} access_len={len(access)} refresh_len={len(refresh)} "
        f"expires_in={payload.get('expires_in')} api_base={account_base}",
        flush=True,
    )
    permissions_db.set_kommo_oauth_tokens(
        access_token=encrypt_secret(access, jwt_secret),
        refresh_token=encrypt_secret(refresh, jwt_secret) if refresh else "",
        expires_at=expires_at,
        referer=referer,
        auth_mode="oauth",
    )
    permissions_db.set_kommo_integration(
        enabled=True,
        client_id=client_id,
        client_secret=None,
        redirect_uri=redirect_uri,
        subdomain=domain,
    )

    # Probe the same token clients will use (encrypt → decrypt roundtrip), not raw exchange bytes.
    access_for_probe = _decrypt_access_token(
        encrypt_secret(access, jwt_secret),
        jwt_secret,
    )
    if access_for_probe != access:
        print(
            f"[kommo] WARNING: access_token encrypt roundtrip mismatch "
            f"(raw_len={len(access)} db_len={len(access_for_probe)})",
            flush=True,
        )
        access_for_probe = access

    cfg_after = permissions_db.get_kommo_integration()
    tokens_after = permissions_db.get_kommo_oauth_tokens()
    domain_info = await fetch_account_domain_info(access_for_probe, refresh=refresh)
    if domain_info:
        print(
            f"[kommo] OAuth domain resolved: domain={domain_info.get('domain')!r} "
            f"subdomain={domain_info.get('subdomain')!r}",
            flush=True,
        )
    account_base, probe_details = await _find_working_account_base(
        access_for_probe,
        cfg=cfg_after,
        tokens=tokens_after,
        prefer=account_base,
        refresh=refresh,
        domain_info=domain_info,
        probe_attempts=4,
        probe_retry_delay_s=1.0,
    )

    best_guess = account_base or account_base_from_domain_info(domain_info) or build_softphone_api_web_base_url(domain)
    oauth_domain_confirmed = bool(domain_info and domain_info.get("domain"))
    client_match = _client_id_matches_jwt(cfg_after, access_for_probe)
    diagnosis = _kommo_token_diagnosis(access_for_probe, cfg_after)

    if not account_base:
        detail = _format_probe_details(probe_details)
        jwt_info = decode_jwt_payload(access_for_probe)
        print(
            f"[kommo] OAuth tokens saved; account API probe failed: {detail} "
            f"jwt_account_id={jwt_info.get('account_id')} jwt_api_domain={jwt_info.get('api_domain')!r} "
            f"jwt_keys={diagnosis.get('jwt', {}).get('payload_keys')} "
            f"jwt_scopes={diagnosis.get('jwt', {}).get('scopes')!r} "
            f"crm_api_in_jwt={diagnosis.get('crm_api_in_jwt')} "
            f"exchange_via={exchange_via} client_id_matches_jwt={client_match}",
            flush=True,
        )
        resolved_base = best_guess
        permissions_db.set_kommo_oauth_tokens(
            access_token=encrypt_secret(access, jwt_secret),
            refresh_token=encrypt_secret(refresh, jwt_secret) if refresh else "",
            expires_at=expires_at,
            referer=referer or "",
            account_base_url=resolved_base,
            auth_mode="oauth",
        )
        return {
            "subdomain": domain,
            "domain": domain,
            "expires_at": expires_at,
            "account_base_url": resolved_base,
            "probe_ok": False,
            "oauth_domain_ok": oauth_domain_confirmed,
            "account_probe_ok": False,
            "client_id_matches_jwt": client_match,
            "exchange_via": exchange_via,
            "probe_details": detail,
            "domain_resolved": oauth_domain_confirmed,
            "diagnosis": diagnosis,
            "message": _account_api_unavailable_message(
                probe_details=probe_details,
                jwt_info=diagnosis.get("jwt"),
                diagnosis=diagnosis,
            ),
        }

    permissions_db.set_kommo_oauth_tokens(
        access_token=encrypt_secret(access, jwt_secret),
        refresh_token=encrypt_secret(refresh, jwt_secret) if refresh else "",
        expires_at=expires_at,
        referer=referer or "",
        account_base_url=account_base,
        auth_mode="oauth",
    )

    return {
        "subdomain": domain,
        "domain": domain,
        "expires_at": expires_at,
        "account_base_url": account_base,
        "probe_ok": True,
    }


def _looks_like_account_json(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(k in payload for k in ("id", "name", "subdomain", "current_user_id"))


async def _probe_kommo_access_token_detail(
    access: str,
    account_base: str,
    *,
    attempts: int = 1,
    retry_delay_s: float = 0.0,
) -> dict:
    token = sanitize_access_token(access or "")
    base = (account_base or "").strip().rstrip("/")
    result = {
        "account_base_url": base,
        "ok": False,
        "status_code": None,
        "content_type": "",
        "is_json": False,
        "account_id": None,
        "body_preview": "",
        "error": "",
    }
    if not token or not base:
        result["error"] = "missing token or account base"
        return result

    last_result = result
    tries = max(1, int(attempts))
    for attempt in range(1, tries + 1):
        attempt_result = await probe_kommo_account_api(base, token)
        attempt_result["attempt"] = attempt
        if attempt_result.get("ok"):
            if attempt_result.get("transport") == "curl":
                print(f"[kommo] token probe OK via curl for {base}", flush=True)
            return attempt_result
        last_result = attempt_result

        if attempt < tries and retry_delay_s > 0:
            print(
                f"[kommo] token probe retry {attempt}/{tries} for {base} "
                f"after {retry_delay_s}s (last: {last_result.get('error')})",
                flush=True,
            )
            await asyncio.sleep(retry_delay_s)

    return last_result


async def _probe_kommo_access_token(access: str, account_base: str) -> bool:
    detail = await _probe_kommo_access_token_detail(access, account_base)
    if not detail["ok"]:
        print(
            f"[kommo] token probe failed for {detail.get('account_base_url')}: "
            f"{detail.get('error')} status={detail.get('status_code')} "
            f"body={detail.get('body_preview')}",
            flush=True,
        )
    return bool(detail["ok"])


def _format_probe_details(details: list[dict]) -> str:
    if not details:
        return "no domains tried"
    parts = []
    for d in details:
        base = d.get("account_base_url") or "?"
        if d.get("ok"):
            parts.append(f"{base}: OK")
        else:
            err = d.get("error") or "failed"
            if d.get("status_code"):
                err += f" (HTTP {d.get('status_code')})"
            preview = (d.get("body_preview") or "").strip()
            if preview and d.get("status_code") == 401:
                err += f" — {preview[:120]}"
            parts.append(f"{base}: {err}")
    return "; ".join(parts)


async def _find_working_account_base(
    access: str,
    *,
    cfg: dict,
    tokens: dict,
    prefer: str | None = None,
    refresh: str = "",
    domain_info: dict | None = None,
    probe_attempts: int = 3,
    probe_retry_delay_s: float = 1.5,
) -> tuple[str, list[dict]]:
    if domain_info is None:
        domain_info = await fetch_account_domain_info(access, refresh=refresh)
    details: list[dict] = []
    for base in _account_base_candidates(
        cfg,
        tokens,
        prefer=prefer,
        domain_info=domain_info,
        access=access,
    ):
        detail = await _probe_kommo_access_token_detail(
            access,
            base,
            attempts=probe_attempts,
            retry_delay_s=probe_retry_delay_s,
        )
        details.append(detail)
        if detail["ok"]:
            return base, details
    return "", details


async def _refresh_tokens_if_needed(*, jwt_secret: str, force: bool = False) -> dict:
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    if not bool(cfg.get("enabled")):
        raise ValueError("Kommo integration is disabled")
    if not tokens.get("access_token"):
        raise ValueError("Kommo is not authorized")

    if not force and not _token_needs_refresh(tokens.get("expires_at")):
        return tokens

    subdomain = normalize_domain_field(cfg.get("subdomain") or "")
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = _decrypt_client_secret(cfg.get("client_secret") or "", jwt_secret)
    refresh_enc = tokens.get("refresh_token") or ""
    refresh = decrypt_secret(refresh_enc, jwt_secret) if refresh_enc else ""
    if not subdomain or not client_id or not client_secret or not refresh:
        missing = []
        if not subdomain:
            missing.append("subdomain")
        if not client_id:
            missing.append("client_id")
        if not client_secret:
            missing.append("client_secret")
        if not refresh:
            missing.append("refresh_token")
        raise ValueError(
            "Cannot refresh Kommo token — missing "
            + ", ".join(missing)
            + ". Re-enter Client Secret, Save, then Disconnect and Authorize again."
        )

    redirect_uri = normalize_redirect_uri((cfg.get("redirect_uri") or "").strip())
    if not redirect_uri:
        raise ValueError(
            "Cannot refresh Kommo token — redirect_uri missing in gateway settings. "
            "Save Redirect URI (must match Kommo integration exactly), then Authorize again."
        )

    print(f"[kommo] refreshing OAuth token for {subdomain} (force={force})", flush=True)
    payload = await refresh_access_token(
        subdomain=subdomain,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh,
        redirect_uri=redirect_uri,
        referer=tokens.get("referer") or None,
    )
    access = sanitize_access_token(payload.get("access_token") or "")
    new_refresh = sanitize_access_token(payload.get("refresh_token") or refresh)
    if not access:
        raise ValueError("Kommo token refresh failed")

    expires_at = expires_at_from_seconds(payload.get("expires_in"))
    permissions_db.set_kommo_oauth_tokens(
        access_token=encrypt_secret(access, jwt_secret),
        refresh_token=encrypt_secret(new_refresh, jwt_secret) if new_refresh else "",
        expires_at=expires_at,
        referer=tokens.get("referer") or "",
        account_base_url=tokens.get("account_base_url") or "",
    )
    return permissions_db.get_kommo_oauth_tokens()


def get_client_status(*, jwt_secret: str, extension: str | None = None) -> dict:
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    enabled = bool(cfg.get("enabled"))
    secret_ok = bool((cfg.get("client_secret") or "").strip())
    configured = bool((cfg.get("client_id") or "").strip()) and secret_ok
    authorized = bool(tokens.get("access_token"))
    subdomain = cfg.get("subdomain") or ""
    expires_at = tokens.get("expires_at")
    expired = authorized and _token_needs_refresh(expires_at, skew_seconds=0)
    status = {
        "available": enabled and configured and authorized and not expired,
        "enabled": enabled,
        "configured": configured,
        "authorized": authorized,
        "subdomain": subdomain,
        "token_expires_at": expires_at,
        "token_expired": expired,
        "needs_reauthorize": authorized and expired,
    }
    return _apply_extension_exclusion(status, extension)


async def get_client_status_async(*, jwt_secret: str, extension: str | None = None) -> dict:
    status = get_client_status(jwt_secret=jwt_secret, extension=extension)
    status = _apply_extension_exclusion(status, extension)
    if status.get("excluded"):
        return status
    if not status["authorized"]:
        return status

    try:
        session = await get_client_session(jwt_secret=jwt_secret, extension=extension)
        status["available"] = True
        status["token_valid"] = True
        status["needs_reauthorize"] = False
        status["account_base_url"] = session.get("account_base_url") or ""
    except ValueError as exc:
        status["available"] = False
        status["token_valid"] = False
        status["needs_reauthorize"] = True
        status["error"] = str(exc)
    except Exception as exc:
        status["available"] = False
        status["token_valid"] = False
        status["error"] = str(exc)

    return _apply_extension_exclusion(status, extension)


def get_kommo_storage_debug(*, jwt_secret: str) -> dict:
    """What is stored in SQLite (for admin troubleshooting — never returns full secrets)."""
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    access_enc = (tokens.get("access_token") or "").strip()
    refresh_enc = (tokens.get("refresh_token") or "").strip()
    access_plain = _decrypt_access_token(access_enc, jwt_secret) if access_enc else ""
    roundtrip_ok = False
    if access_plain:
        try:
            roundtrip_ok = (
                _decrypt_access_token(encrypt_secret(access_plain, jwt_secret), jwt_secret)
                == access_plain
            )
        except Exception:
            roundtrip_ok = False
    return {
        "subdomain": (cfg.get("subdomain") or "").strip(),
        "auth_mode": (tokens.get("auth_mode") or "oauth").strip(),
        "has_access_token": bool(access_enc),
        "has_refresh_token": bool(refresh_enc),
        "access_token_enc_len": len(access_enc),
        "access_token_plain_len": len(access_plain),
        "access_token_prefix": access_plain[:10] if access_plain else "",
        "looks_like_jwt": access_plain.startswith("eyJ") and access_plain.count(".") >= 2,
        "authorized_at": tokens.get("authorized_at"),
        "expires_at": tokens.get("expires_at"),
        "referer": tokens.get("referer") or "",
        "account_base_url": tokens.get("account_base_url") or "",
        "client_secret_configured": bool((cfg.get("client_secret") or "").strip()),
        "decrypt_roundtrip_ok": roundtrip_ok,
    }


async def get_client_session(*, jwt_secret: str, force_refresh: bool = False, extension: str | None = None) -> dict:
    if extension and permissions_db.is_kommo_extension_excluded(extension):
        raise ValueError("Kommo integration is not available for this extension")

    status = get_client_status(jwt_secret=jwt_secret, extension=extension)
    if not status["enabled"]:
        raise ValueError("Kommo integration is disabled on gateway")
    if not status["configured"]:
        raise ValueError("Kommo integration is not configured on gateway")
    if not status["authorized"]:
        raise ValueError("Kommo is not authorized on gateway — ask admin to authorize")

    tokens = await _refresh_tokens_if_needed(jwt_secret=jwt_secret, force=force_refresh)
    cfg = permissions_db.get_kommo_integration()
    access = _decrypt_access_token(tokens.get("access_token") or "", jwt_secret)
    if not access:
        raise ValueError("Kommo access token is missing")

    refresh_enc = tokens.get("refresh_token") or ""
    refresh = decrypt_secret(refresh_enc, jwt_secret) if refresh_enc else ""
    subdomain = cfg.get("subdomain") or ""

    oauth_confirmed, domain_info = await _confirm_oauth_token(access, refresh=refresh)
    account_base, probe_details = await _find_working_account_base(
        access,
        cfg=cfg,
        tokens=tokens,
        domain_info=domain_info,
    )

    if not account_base:
        print(
            "[kommo] stored access token rejected by account API, forcing refresh. "
            + _format_probe_details(probe_details),
            flush=True,
        )
        try:
            tokens = await _refresh_tokens_if_needed(jwt_secret=jwt_secret, force=True)
        except Exception as exc:
            raise ValueError(f"Kommo token is invalid and refresh failed: {exc}") from exc
        access = _decrypt_access_token(tokens.get("access_token") or "", jwt_secret)
        if not access:
            raise ValueError("Kommo access token is missing after refresh")
        refresh_enc = tokens.get("refresh_token") or ""
        refresh = decrypt_secret(refresh_enc, jwt_secret) if refresh_enc else ""
        oauth_confirmed, domain_info = await _confirm_oauth_token(access, refresh=refresh)
        account_base, probe_details = await _find_working_account_base(
            access,
            cfg=cfg,
            tokens=tokens,
            domain_info=domain_info,
        )
        if not account_base:
            detail = _format_probe_details(probe_details)
            diagnosis = _kommo_token_diagnosis(access, cfg)
            raise ValueError(
                _account_api_unavailable_message(
                    probe_details=detail,
                    jwt_info=diagnosis.get("jwt"),
                    diagnosis=diagnosis,
                )
            )

    # Clients use the same API host as local OAuth (subdomain → .amocrm.ru), not OAuth shard hosts.
    sub = normalize_subdomain(cfg.get("subdomain") or "") or normalize_subdomain(subdomain)
    softphone_base = build_softphone_api_web_base_url(sub)
    account_base_for_client = softphone_base if softphone_base else account_base

    if account_base_for_client != (tokens.get("account_base_url") or "").strip().rstrip("/"):
        permissions_db.set_kommo_oauth_tokens(
            access_token=tokens.get("access_token") or "",
            refresh_token=tokens.get("refresh_token") or "",
            expires_at=tokens.get("expires_at"),
            referer=tokens.get("referer") or "",
            account_base_url=account_base_for_client,
        )

    acting = await _acting_kommo_user_for_extension(
        access=access,
        account_base=account_base_for_client,
        extension=extension,
    )

    return {
        "subdomain": subdomain,
        "access_token": access,
        "expires_at": tokens.get("expires_at"),
        "account_base_url": account_base_for_client,
        "kommo_user_id": acting.get("kommo_user_id"),
        "kommo_user_name": acting.get("kommo_user_name") or "",
        "kommo_user_id_source": acting.get("kommo_user_id_source") or "",
    }


async def admin_test_kommo(*, jwt_secret: str, override_access_token: str | None = None) -> dict:
    async with _kommo_ops_lock:
        return await _admin_test_kommo_impl(
            jwt_secret=jwt_secret,
            override_access_token=override_access_token,
        )


async def _admin_test_kommo_impl(*, jwt_secret: str, override_access_token: str | None = None) -> dict:
    cfg = permissions_db.get_kommo_integration()
    tokens = permissions_db.get_kommo_oauth_tokens()
    configured = bool((cfg.get("client_id") or "").strip() and (cfg.get("client_secret") or ""))
    authorized = bool(tokens.get("access_token"))
    account_base = _account_base_for_cfg(cfg, tokens)
    result: dict = {
        "impl_version": KOMMO_IMPL_VERSION,
        "storage": get_kommo_storage_debug(jwt_secret=jwt_secret),
        "configured": configured,
        "authorized": authorized,
        "subdomain": cfg.get("subdomain") or "",
        "account_base_url": account_base,
        "oauth_domain_ok": False,
        "account_probe_ok": False,
        "token_valid": False,
        "probe_ok": False,
        "refresh_attempted": False,
        "refresh_ok": False,
        "probe_ok_after_refresh": False,
        "refresh_error": None,
        "message": "",
        "probed_from": "database",
    }
    if not configured:
        result["message"] = "Save Client ID and Client Secret first."
        return result
    if not authorized and not override_access_token:
        result["message"] = "Not authorized — click Authorize with Kommo."
        return result

    if override_access_token:
        access = sanitize_access_token(override_access_token)
        result["probed_from"] = "override"
    else:
        access = _decrypt_access_token(tokens.get("access_token") or "", jwt_secret)
    result["access_token_len"] = len(access)
    result["client_secret_len"] = len(_decrypt_client_secret(cfg.get("client_secret") or "", jwt_secret))
    result["referer"] = tokens.get("referer") or ""
    result["jwt"] = jwt_debug_info(access)
    diagnosis = _kommo_token_diagnosis(access, cfg)
    result["diagnosis"] = diagnosis
    result["crm_api_in_jwt"] = diagnosis.get("crm_api_in_jwt")
    client_match = diagnosis.get("client_id_matches_jwt")
    result["client_id_matches_jwt"] = client_match
    if client_match is False:
        result["message"] = (
            "JWT client_uuid does not match gateway Client ID — token is from a different Kommo integration."
        )
        return result

    refresh_enc = tokens.get("refresh_token") or ""
    refresh = decrypt_secret(refresh_enc, jwt_secret) if refresh_enc else ""
    oauth_confirmed, domain_info = await _confirm_oauth_token(access, refresh=refresh)
    result["oauth_domain_ok"] = oauth_confirmed
    if domain_info:
        result["oauth_domain"] = domain_info.get("domain") or domain_info.get("subdomain")

    working_base, probe_details = await _find_working_account_base(
        access,
        cfg=cfg,
        tokens=tokens,
        domain_info=domain_info,
    )
    result["probe_details"] = probe_details
    result["account_probe_ok"] = bool(working_base)
    result["probe_ok"] = bool(working_base)

    if working_base:
        result["account_base_url"] = working_base
        result["token_valid"] = True
        result["message"] = f"Token OK — account API confirmed for {working_base}"
        if not override_access_token:
            permissions_db.set_kommo_oauth_tokens(
                access_token=tokens.get("access_token") or "",
                refresh_token=tokens.get("refresh_token") or "",
                expires_at=tokens.get("expires_at"),
                referer=tokens.get("referer") or "",
                account_base_url=working_base,
            )
        return result

    if override_access_token:
        detail = _format_probe_details(probe_details)
        result["message"] = _account_api_unavailable_message(
            probe_details=detail,
            jwt_info=result.get("jwt"),
            diagnosis=result.get("diagnosis"),
        )
        return result

    result["refresh_attempted"] = True
    try:
        await _refresh_tokens_if_needed(jwt_secret=jwt_secret, force=True)
        result["refresh_ok"] = True
        tokens = permissions_db.get_kommo_oauth_tokens()
        access = _decrypt_access_token(tokens.get("access_token") or "", jwt_secret)
        result["access_token_len_after_refresh"] = len(access)
        result["jwt"] = jwt_debug_info(access)
        refresh_enc = tokens.get("refresh_token") or ""
        refresh = decrypt_secret(refresh_enc, jwt_secret) if refresh_enc else ""
        oauth_confirmed, domain_info = await _confirm_oauth_token(access, refresh=refresh)
        result["oauth_domain_ok"] = oauth_confirmed
        working_base, probe_details_after = await _find_working_account_base(
            access,
            cfg=cfg,
            tokens=tokens,
            domain_info=domain_info,
        )
        result["probe_details_after_refresh"] = probe_details_after
        result["account_probe_ok"] = bool(working_base)
        result["probe_ok_after_refresh"] = bool(working_base)
        result["probe_ok"] = bool(working_base)
        if working_base:
            result["account_base_url"] = working_base
            result["token_valid"] = True
            result["message"] = f"Token OK after refresh — account API confirmed for {working_base}"
            permissions_db.set_kommo_oauth_tokens(
                access_token=tokens.get("access_token") or "",
                refresh_token=tokens.get("refresh_token") or "",
                expires_at=tokens.get("expires_at"),
                referer=tokens.get("referer") or "",
                account_base_url=working_base,
            )
        else:
            detail = _format_probe_details(probe_details_after)
            diagnosis = _kommo_token_diagnosis(access, cfg)
            result["diagnosis"] = diagnosis
            result["crm_api_in_jwt"] = diagnosis.get("crm_api_in_jwt")
            result["message"] = _account_api_unavailable_message(
                probe_details=detail,
                jwt_info=result.get("jwt"),
                diagnosis=diagnosis,
            )
    except Exception as exc:
        result["refresh_error"] = str(exc)
        result["message"] = f"Refresh failed: {exc}"
    return result
