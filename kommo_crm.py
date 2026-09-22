"""Kommo / AmoCRM API v4 client for PBX Gateway process-call pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

import httpx

log = logging.getLogger("kommo_crm")

AMO_MISSED_CALL_RESULT = "No Answer"
AMO_MISSED_CALL_STATUS = 6
_RATE_INTERVAL = 0.5
_last_request_at = 0.0

# Tracking/integration tags auto-added to leads in Kommo (Yandex Metrica, etc.).
_NOISE_LEAD_TAG_PREFIXES = (
    "_ym_uid_",
    "_ym_counter_",
    "_ym_",
    "_ga_",
    "_fbp_",
    "_fbc_",
)
_UUID_TAG_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_noise_lead_tag(name: str) -> bool:
    """Hide auto-tracking tags from admin pickers (not useful for missed-call rules)."""
    n = (name or "").strip()
    if not n:
        return True
    lower = n.lower()
    for prefix in _NOISE_LEAD_TAG_PREFIXES:
        if lower.startswith(prefix):
            return True
    if _UUID_TAG_RE.match(n):
        return True
    # e.g. _ym_uid_1615483272928719271 — underscore prefix + long numeric tail
    if re.match(r"^_[a-z0-9_]+_\d{10,}$", lower):
        return True
    # Import / chat paste junk often saved as tags in Amo.
    if ".csv" in lower:
        return True
    if re.match(r"^\[\d{1,2}:\d{2}\]", n):
        return True
    return False


@dataclass
class ProcessCallOutcome:
    success: bool
    upload_status: str  # uploaded | not_uploaded | failed
    reason: Optional[str] = None
    lead_id: Optional[int] = None
    contact_id: Optional[int] = None
    upload_source: Optional[str] = None


@dataclass
class LeadScore:
    lead_id: int
    is_open: bool
    is_responsible: bool
    is_our_contact_with_phone: bool
    is_main: bool
    is_single_contact: bool
    is_primary: bool
    updated_at: datetime

    def sort_key(self) -> tuple:
        return (
            self.is_open,
            self.is_responsible,
            self.is_our_contact_with_phone,
            self.is_main,
            self.is_single_contact,
            self.is_primary,
            self.updated_at,
            self.lead_id,
        )


class KommoCrmClient:
    def __init__(
        self,
        subdomain: str,
        access_token: str,
        *,
        account_base_url: Optional[str] = None,
        acting_user_id: Optional[int] = None,
        token_refresher: Optional[Callable[[], Awaitable[tuple[str, Optional[str]]]]] = None,
    ) -> None:
        self.subdomain = subdomain.strip()
        self.access_token = access_token
        self.account_base_url = self._resolve_api_base(account_base_url, self.subdomain)
        self.acting_user_id = acting_user_id
        self._token_refresher = token_refresher
        self._drive_url: Optional[str] = None
        self._current_user_id: Optional[int] = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=30.0))
        # Drive multipart uploads can be slow; desktop uses 300s timeout + retries.
        self._upload_http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0))

    @staticmethod
    def _resolve_api_base(account_base_url: Optional[str], subdomain: str) -> str:
        """Match desktop ``GetApiBaseUrl()`` — always ``…/api/v4``."""
        raw = (account_base_url or "").strip().rstrip("/")
        if raw:
            return raw if raw.endswith("/api/v4") else f"{raw}/api/v4"
        sub = (subdomain or "").strip().lower()
        if not sub:
            return ""
        if sub.startswith("http://") or sub.startswith("https://"):
            host = sub.rstrip("/")
        elif ".kommo.com" in sub or ".amocrm." in sub:
            host = f"https://{sub}"
        else:
            host = f"https://{sub}.kommo.com"
        return f"{host.rstrip('/')}/api/v4"

    def _default_api_base(self) -> str:
        return self._resolve_api_base(None, self.subdomain)

    async def close(self) -> None:
        await self._http.aclose()
        await self._upload_http.aclose()

    async def _rate_limit(self) -> None:
        global _last_request_at
        now = time.monotonic()
        wait = _RATE_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()

    async def _ensure_token(self, force_refresh: bool = False) -> None:
        if force_refresh and self._token_refresher:
            token, _ = await self._token_refresher()
            if token:
                self.access_token = token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        content: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        await self._ensure_token()
        await self._rate_limit()
        url = path if path.startswith("http") else f"{self.account_base_url.rstrip('/')}/{path.lstrip('/')}"
        hdrs = {"Authorization": f"Bearer {self.access_token}"}
        if headers:
            hdrs.update(headers)
        resp = await self._http.request(
            method, url, json=json_body, params=params, content=content, headers=hdrs
        )
        if resp.status_code == 401 and self._token_refresher:
            await self._ensure_token(force_refresh=True)
            hdrs["Authorization"] = f"Bearer {self.access_token}"
            resp = await self._http.request(
                method, url, json=json_body, params=params, content=content, headers=hdrs
            )
        return resp

    async def load_account_info(self) -> None:
        resp = await self._request("GET", "/account", params={"with": "drive_url"})
        if resp.status_code != 200:
            log.warning("GET /account failed: %s %s", resp.status_code, resp.text[:200])
            return
        data = resp.json()
        self._drive_url = (data.get("drive_url") or "").rstrip("/")
        self._current_user_id = data.get("current_user_id")
        if self.acting_user_id is None:
            self.acting_user_id = self._current_user_id

    def get_acting_user_id(self) -> Optional[int]:
        return self.acting_user_id or self._current_user_id

    def _is_lead_assigned_to_acting_user(
        self, responsible_user_id: Any, *, strict: bool = True
    ) -> bool:
        """Auto-upload: only deals where responsible_user_id is the mapped Kommo user."""
        acting = self.get_acting_user_id()
        if acting is None:
            return not strict
        if responsible_user_id is None:
            return False
        try:
            return int(responsible_user_id) == int(acting)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def normalize_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        return digits

    @staticmethod
    def phone_search_variants(normalized: str) -> list[str]:
        if not normalized:
            return []
        variants = [normalized]
        if len(normalized) >= 10:
            variants.append(normalized[-10:])
        if len(normalized) == 11 and normalized.startswith("7"):
            variants.append("+" + normalized)
            variants.append("8" + normalized[1:])
        if len(normalized) == 11 and normalized.startswith("1"):
            variants.append("+" + normalized)
        if len(normalized) == 10 and not normalized.startswith("7"):
            variants.append("1" + normalized)
            variants.append("+1" + normalized)
        return list(dict.fromkeys(variants))

    @staticmethod
    def phone_query_variants(normalized: str) -> list[str]:
        """Match desktop ``EnumeratePhoneSearchQueries`` (RU +E164 variants)."""
        if not normalized:
            return []
        variants = [normalized]
        if len(normalized) >= 10 and normalized.startswith("7"):
            variants.append("+" + normalized)
            variants.append("8" + normalized[1:])
        if len(normalized) >= 10:
            variants.append(normalized[-10:])
        return list(dict.fromkeys(variants))

    async def find_contact_by_phone(self, phone: str) -> Optional[int]:
        normalized = self.normalize_phone(phone)
        if not normalized:
            return None
        variants = list(dict.fromkeys(self.phone_query_variants(normalized) + self.phone_search_variants(normalized)))
        for variant in variants:
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(0.25 + 0.22 * (attempt - 1))
                resp = await self._request(
                    "GET",
                    "/contacts",
                    params={"query": variant, "limit": 10},
                )
                if resp.status_code == 204:
                    break
                if resp.status_code != 200:
                    log.warning("contact search %s failed: %s", variant, resp.status_code)
                    if attempt >= 2:
                        break
                    continue
                contacts = (resp.json().get("_embedded") or {}).get("contacts") or []
                if not contacts:
                    break
                for c in contacts:
                    cid = c.get("id")
                    if not cid:
                        continue
                    if self._contact_has_phone(c, normalized):
                        print(
                            f"[kommo_crm] contact {cid} matched phone {phone} (embedded)",
                            flush=True,
                        )
                        return int(cid)
                    full = await self._get_contact(int(cid))
                    if full and self._contact_has_phone(full, normalized):
                        print(
                            f"[kommo_crm] contact {cid} matched phone {phone} (full card)",
                            flush=True,
                        )
                        return int(cid)
                break
        return None

    async def lookup_contact_display(self, phone: str) -> dict[str, Any]:
        """Like desktop GetContactNameByPhoneAsync + open lead for upload."""
        contact_id = await self.find_contact_by_phone(phone)
        name: Optional[str] = None
        lead_id: Optional[int] = None

        if contact_id:
            contact = await self._get_contact(contact_id)
            if contact:
                name = (contact.get("name") or "").strip() or None
            lead_id = await self.find_open_lead_for_contact(contact_id)

        if not lead_id:
            lead_id = await self.find_best_lead_by_phone(phone)

        if not name and lead_id:
            lead = await self.get_lead(lead_id)
            if lead:
                name = (lead.get("name") or "").strip() or None

        if name or lead_id or contact_id:
            print(
                f"[kommo_crm] lookup {phone}: name={name!r} contact={contact_id} lead={lead_id}",
                flush=True,
            )
        else:
            print(f"[kommo_crm] lookup miss for {phone}", flush=True)

        return {
            "name": name,
            "contact_id": contact_id,
            "lead_id": lead_id,
        }

    async def _get_contact(self, contact_id: int) -> Optional[dict]:
        resp = await self._request("GET", f"/contacts/{contact_id}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 204:
            resp = await self._request(
                "GET",
                "/contacts",
                params={"filter[id][]": contact_id, "limit": 1},
            )
            if resp.status_code == 200:
                contacts = (resp.json().get("_embedded") or {}).get("contacts") or []
                return contacts[0] if contacts else None
        return None

    def _contact_has_phone(self, contact: dict, normalized: str) -> bool:
        for cf in (contact.get("custom_fields_values") or []):
            if (cf.get("field_code") or "").upper() != "PHONE":
                continue
            for v in cf.get("values") or []:
                val = self.normalize_phone(str(v.get("value") or ""))
                if val and (val == normalized or val.endswith(normalized[-10:])):
                    return True
        return False

    @staticmethod
    def _parse_ts(token: Any) -> datetime:
        if token is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(token, int):
            return datetime.fromtimestamp(token, tz=timezone.utc)
        if isinstance(token, str):
            try:
                return datetime.fromisoformat(token.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_copy_lead(name: str) -> bool:
        n = (name or "").lower()
        return "копия" in n or "copy" in n

    async def find_open_lead_for_contact(self, contact_id: int) -> Optional[int]:
        acting = self.get_acting_user_id()
        resp = await self._request(
            "GET",
            f"/contacts/{contact_id}",
            params={"with": "leads", "limit": 50, "order[created_at]": "desc"},
        )
        if resp.status_code != 200:
            return None
        leads = (resp.json().get("_embedded") or {}).get("leads") or []
        open_leads: list[tuple[int, datetime]] = []
        for short in leads[:30]:
            lid = short.get("id")
            if not lid:
                continue
            lead = short
            # Kommo embeds contact leads as id+links only (no closed_at/created_at/name).
            # Desktop loads the full lead in that case — without it every lead looks
            # "open" with equal timestamps and the oldest one wins. Match desktop.
            if (
                "closed_at" not in lead
                or "created_at" not in lead
                or "responsible_user_id" not in lead
            ):
                full = await self.get_lead(int(lid))
                if not full:
                    continue
                lead = full
            name = lead.get("name") or ""
            if self._is_copy_lead(name):
                continue
            if lead.get("closed_at") is not None:
                continue
            if not self._is_lead_assigned_to_acting_user(lead.get("responsible_user_id")):
                print(
                    f"[kommo_crm] skip lead {lid} for contact {contact_id}: "
                    f"responsible={lead.get('responsible_user_id')} acting={acting}",
                    flush=True,
                )
                continue
            created = self._parse_ts(lead.get("created_at"))
            open_leads.append((int(lid), created))
        if not open_leads:
            return None
        open_leads.sort(key=lambda x: x[1], reverse=True)
        picked = open_leads[0][0]
        print(
            f"[kommo_crm] own open lead {picked} for contact {contact_id} "
            f"(acting user={acting})",
            flush=True,
        )
        return picked

    async def list_open_leads_for_contact(
        self, contact_id: int
    ) -> list[dict[str, Any]]:
        """All open leads for a contact owned by the acting Kommo user."""
        acting = self.get_acting_user_id()
        resp = await self._request(
            "GET",
            f"/contacts/{contact_id}",
            params={"with": "leads", "limit": 50, "order[created_at]": "desc"},
        )
        if resp.status_code != 200:
            return []
        leads = (resp.json().get("_embedded") or {}).get("leads") or []
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        for short in leads[:30]:
            lid = short.get("id")
            if not lid:
                continue
            lead = short
            if (
                "closed_at" not in lead
                or "created_at" not in lead
                or "responsible_user_id" not in lead
            ):
                full = await self.get_lead(int(lid))
                if not full:
                    continue
                lead = full
            name = lead.get("name") or ""
            if self._is_copy_lead(name):
                continue
            if lead.get("closed_at") is not None:
                continue
            if not self._is_lead_assigned_to_acting_user(lead.get("responsible_user_id")):
                continue
            lead_id = int(lid)
            if lead_id in seen:
                continue
            seen.add(lead_id)
            out.append(
                {
                    "id": lead_id,
                    "name": name or f"Lead #{lead_id}",
                    "contact_id": contact_id,
                    "updated_at": lead.get("updated_at") or lead.get("created_at"),
                }
            )
        out.sort(
            key=lambda item: self._parse_ts(item.get("updated_at")),
            reverse=True,
        )
        return out

    async def resolve_upload_target(
        self, phone: str
    ) -> tuple[Optional[int], Optional[int]]:
        """Contact → newest open lead owned by acting Kommo user → phone fallback."""
        contact_id = await self.find_contact_by_phone(phone)
        if contact_id:
            open_lead = await self.find_open_lead_for_contact(contact_id)
            if open_lead:
                print(
                    f"[kommo_crm] open lead {open_lead} for contact {contact_id} ({phone})",
                    flush=True,
                )
                return open_lead, contact_id
            return None, contact_id

        fallback_lead = await self.find_best_lead_by_phone(phone)
        if fallback_lead:
            print(
                f"[kommo_crm] no contact for {phone}, fallback lead {fallback_lead}",
                flush=True,
            )
            return fallback_lead, None
        print(f"[kommo_crm] no upload target for {phone}", flush=True)
        return None, None

    async def find_best_lead_by_phone(
        self, phone: str, *, open_only: bool = True, own_only: bool = True
    ) -> Optional[int]:
        """Fallback when contact search returns nothing (desktop GetLeadsByPhoneAsync)."""
        normalized = self.normalize_phone(phone)
        if not normalized:
            return None
        variants = list(dict.fromkeys(self.phone_query_variants(normalized) + self.phone_search_variants(normalized)))
        contact_cache: dict[int, Optional[dict]] = {}
        for variant in variants:
            resp = await self._request(
                "GET",
                "/leads",
                params={
                    "query": variant,
                    "limit": 30,
                    "order[updated_at]": "desc",
                    "with": "contacts",
                },
            )
            if resp.status_code == 204:
                continue
            if resp.status_code != 200:
                log.warning("lead search by phone failed: %s", resp.status_code)
                continue
            leads = (resp.json().get("_embedded") or {}).get("leads") or []
            picked = await self._pick_lead_from_search_results(
                leads,
                normalized,
                open_only=open_only,
                own_only=own_only,
                contact_cache=contact_cache,
            )
            if picked:
                print(
                    f"[kommo_crm] lead {picked} matched phone {phone} "
                    f"(open_only={open_only}, own_only={own_only})",
                    flush=True,
                )
                return picked
        if open_only and not own_only:
            return await self.find_best_lead_by_phone(phone, open_only=False, own_only=False)
        return None

    async def list_open_leads_for_phone(
        self, phone: str, *, own_only: bool = True, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Open Kommo leads matching a phone number (for admin upload picker)."""
        normalized = self.normalize_phone(phone)
        if not normalized:
            return []
        contact_id = await self.find_contact_by_phone(phone)
        if contact_id:
            items = await self.list_open_leads_for_contact(contact_id)
            if items:
                return items[:limit]

        variants = list(
            dict.fromkeys(
                self.phone_query_variants(normalized)
                + self.phone_search_variants(normalized)
            )
        )
        contact_cache: dict[int, Optional[dict]] = {}
        collected: list[dict[str, Any]] = []
        seen: set[int] = set()
        for variant in variants:
            resp = await self._request(
                "GET",
                "/leads",
                params={
                    "query": variant,
                    "limit": 30,
                    "order[updated_at]": "desc",
                    "with": "contacts",
                },
            )
            if resp.status_code == 204:
                continue
            if resp.status_code != 200:
                log.warning("lead search by phone failed: %s", resp.status_code)
                continue
            leads = (resp.json().get("_embedded") or {}).get("leads") or []
            for item in await self._collect_leads_from_search_results(
                leads,
                normalized,
                open_only=True,
                own_only=own_only,
                contact_cache=contact_cache,
            ):
                lid = int(item["id"])
                if lid in seen:
                    continue
                seen.add(lid)
                collected.append(item)
                if len(collected) >= limit:
                    return collected
        collected.sort(
            key=lambda item: self._parse_ts(item.get("updated_at")),
            reverse=True,
        )
        return collected[:limit]

    async def _collect_leads_from_search_results(
        self,
        leads: list[dict],
        normalized_phone: str,
        *,
        open_only: bool,
        own_only: bool,
        contact_cache: dict[int, Optional[dict]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for lead in leads[:30]:
            lid = lead.get("id")
            if not lid:
                continue
            name = lead.get("name") or ""
            if self._is_copy_lead(name):
                continue
            is_open = lead.get("closed_at") is None
            if not is_open and open_only:
                continue

            lead_for_owner = lead
            contacts = ((lead.get("_embedded") or {}).get("contacts")) or []
            if not contacts or (own_only and "responsible_user_id" not in lead):
                full = await self.get_lead_with_contacts(int(lid))
                if full:
                    lead_for_owner = full
                    contacts = (((full or {}).get("_embedded") or {}).get("contacts")) or []
            if not contacts:
                continue
            if own_only and not self._is_lead_assigned_to_acting_user(
                lead_for_owner.get("responsible_user_id")
            ):
                continue

            has_phone = False
            matched_contact_id: Optional[int] = None
            contact_count = 0
            for contact in contacts:
                cid = contact.get("id")
                if not cid:
                    continue
                contact_count += 1
                cid = int(cid)
                if cid not in contact_cache:
                    contact_cache[cid] = await self._get_contact(cid)
                full_contact = contact_cache[cid]
                if full_contact and self._contact_has_phone(full_contact, normalized_phone):
                    has_phone = True
                    matched_contact_id = cid
                    break
                if self._contact_has_phone(contact, normalized_phone):
                    has_phone = True
                    matched_contact_id = cid
                    break

            if not has_phone and not open_only and contact_count > 0:
                has_phone = True
            if not has_phone:
                continue
            out.append(
                {
                    "id": int(lid),
                    "name": name or f"Lead #{lid}",
                    "contact_id": matched_contact_id,
                    "updated_at": lead_for_owner.get("updated_at")
                    or lead_for_owner.get("created_at"),
                }
            )
        return out

    async def _pick_lead_from_search_results(
        self,
        leads: list[dict],
        normalized_phone: str,
        *,
        open_only: bool,
        own_only: bool,
        contact_cache: dict[int, Optional[dict]],
    ) -> Optional[int]:
        """Desktop GetLeadsByPhoneAsync: leads come ordered by updated_at desc;
        take the FIRST one that is not a copy, matches the open filter and has a
        contact whose phone actually contains the number."""
        for lead in leads[:30]:
            lid = lead.get("id")
            if not lid:
                continue
            name = lead.get("name") or ""
            if self._is_copy_lead(name):
                continue
            is_open = lead.get("closed_at") is None
            if not is_open and open_only:
                continue

            lead_for_owner = lead
            contacts = ((lead.get("_embedded") or {}).get("contacts")) or []
            if not contacts or (own_only and "responsible_user_id" not in lead):
                full = await self.get_lead_with_contacts(int(lid))
                if full:
                    lead_for_owner = full
                    contacts = (((full or {}).get("_embedded") or {}).get("contacts")) or []
            if not contacts:
                continue
            if own_only and not self._is_lead_assigned_to_acting_user(
                lead_for_owner.get("responsible_user_id")
            ):
                continue

            has_phone = False
            contact_count = 0
            for contact in contacts:
                cid = contact.get("id")
                if not cid:
                    continue
                contact_count += 1
                cid = int(cid)
                if cid not in contact_cache:
                    contact_cache[cid] = await self._get_contact(cid)
                full_contact = contact_cache[cid]
                if full_contact and self._contact_has_phone(full_contact, normalized_phone):
                    has_phone = True
                    break
                # Embedded contact may carry the phone when the full card is 204/unavailable.
                if self._contact_has_phone(contact, normalized_phone):
                    has_phone = True
                    break

            # Desktop: with closed leads allowed, a query-matched lead whose contact
            # cards could not be verified is still accepted.
            if not has_phone and not open_only and contact_count > 0:
                has_phone = True
            if not has_phone:
                continue
            return int(lid)
        return None

    async def get_lead_with_contacts(self, lead_id: int) -> Optional[dict]:
        resp = await self._request(
            "GET", f"/leads/{lead_id}", params={"with": "contacts"}
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 204:
            return await self._get_lead_via_filter(lead_id, with_contacts=True)
        return None

    async def get_lead(self, lead_id: int) -> Optional[dict]:
        resp = await self._request("GET", f"/leads/{lead_id}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 204:
            return await self._get_lead_via_filter(lead_id)
        return None

    async def _get_lead_via_filter(
        self, lead_id: int, *, with_contacts: bool = False
    ) -> Optional[dict]:
        # Some API tokens get 204 on direct GET /leads/{id} even for leads that
        # exist; the list endpoint with filter[id] usually still returns them
        # (same workaround as _get_contact). Without it we'd skip the lead and
        # attach the call to the contact instead.
        params: dict = {"filter[id][]": lead_id, "limit": 1}
        if with_contacts:
            params["with"] = "contacts"
        resp = await self._request("GET", "/leads", params=params)
        if resp.status_code != 200:
            return None
        leads = (resp.json().get("_embedded") or {}).get("leads") or []
        return leads[0] if leads else None

    @staticmethod
    def _call_note_params(
        is_missed: bool, call_from_label: Optional[str]
    ) -> tuple[Optional[str], Optional[int]]:
        caller_line = f"Call from {call_from_label.strip()}" if call_from_label and call_from_label.strip() else None
        if is_missed:
            if caller_line:
                return f"{AMO_MISSED_CALL_RESULT} · {caller_line}", AMO_MISSED_CALL_STATUS
            return AMO_MISSED_CALL_RESULT, AMO_MISSED_CALL_STATUS
        return caller_line, None

    async def process_call(
        self,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: Optional[datetime] = None,
        lead_id: Optional[int] = None,
        call_from_label: Optional[str] = None,
        upload_source: Optional[str] = None,
        call_result: Optional[str] = None,
        call_status: Optional[int] = None,
        did: Optional[str] = None,
        linkedid: Optional[str] = None,
    ) -> ProcessCallOutcome:
        try:
            return await self._process_call_impl(
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                lead_id=lead_id,
                call_from_label=call_from_label,
                upload_source=upload_source,
                call_result=call_result,
                call_status=call_status,
                did=did,
                linkedid=linkedid,
            )
        except httpx.HTTPError as exc:
            log.exception("Kommo HTTP error during process_call")
            print(f"[kommo_crm] process_call HTTP error: {exc}", flush=True)
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                reason=f"Kommo API error: {exc}",
            )
        except Exception as exc:
            log.exception("Unexpected error during process_call")
            print(f"[kommo_crm] process_call error: {exc}", flush=True)
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                reason=str(exc),
            )

    async def _process_call_impl(
        self,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: Optional[datetime] = None,
        lead_id: Optional[int] = None,
        call_from_label: Optional[str] = None,
        upload_source: Optional[str] = None,
        call_result: Optional[str] = None,
        call_status: Optional[int] = None,
        did: Optional[str] = None,
        linkedid: Optional[str] = None,
    ) -> ProcessCallOutcome:
        await self.load_account_info()
        call_time = call_time or datetime.now(timezone.utc)

        from kommo_entity_rules import apply_entity_rule

        entity_ctx = None
        try:
            entity_ctx = await apply_entity_rule(
                self,
                phone=phone,
                is_incoming=is_incoming,
                was_answered=was_answered,
                call_time=call_time,
                did=(did or "").strip(),
                call_from_label=call_from_label or "",
                acting_user_id=self.get_acting_user_id(),
                explicit_lead_id=lead_id,
                linkedid=linkedid,
                entity_source="process_call",
            )
        except Exception as exc:
            log.exception("entity rules failed (call upload continues): %s", exc)
            print(
                f"[kommo_crm] entity rules failed for {phone} (recording/note upload continues): {exc}",
                flush=True,
            )
        if entity_ctx and entity_ctx.lead_id and not lead_id:
            lead_id = entity_ctx.lead_id

        if lead_id:
            return await self._process_for_lead(
                lead_id,
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                call_from_label=call_from_label,
                upload_source=upload_source,
                call_result=call_result,
                call_status=call_status,
            )

        contact_id = await self.find_contact_by_phone(phone)
        if not contact_id:
            fallback_lead = await self.find_best_lead_by_phone(phone)
            if fallback_lead:
                print(
                    f"[kommo_crm] no contact for {phone}, uploading to lead {fallback_lead}",
                    flush=True,
                )
                return await self._process_for_lead(
                    fallback_lead,
                    phone,
                    is_incoming=is_incoming,
                    duration_seconds=duration_seconds,
                    was_answered=was_answered,
                    audio_path=audio_path,
                    call_time=call_time,
                    call_from_label=call_from_label,
                    upload_source=upload_source,
                    call_result=call_result,
                    call_status=call_status,
                )
            return ProcessCallOutcome(
                success=False,
                upload_status="not_uploaded",
                reason="Contact not found in Kommo",
            )

        open_lead = await self.find_open_lead_for_contact(contact_id)
        if open_lead:
            outcome = await self._process_for_lead(
                open_lead,
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                call_from_label=call_from_label,
                upload_source=upload_source,
                call_result=call_result,
                call_status=call_status,
            )
            outcome.contact_id = contact_id
            return outcome

        return await self._process_for_contact(
            contact_id,
            phone,
            is_incoming=is_incoming,
            duration_seconds=duration_seconds,
            was_answered=was_answered,
            audio_path=audio_path,
            call_time=call_time,
            call_from_label=call_from_label,
            upload_source=upload_source,
            call_result=call_result,
            call_status=call_status,
        )

    async def _process_for_lead(
        self,
        lead_id: int,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: datetime,
        call_from_label: Optional[str],
        upload_source: Optional[str],
        call_result: Optional[str] = None,
        call_status: Optional[int] = None,
    ) -> ProcessCallOutcome:
        has_recording = bool(audio_path and Path(audio_path).is_file() and Path(audio_path).stat().st_size > 0)
        is_missed = not was_answered and not has_recording

        if is_missed:
            if call_result:
                cr, cs = call_result, call_status
            else:
                cr, cs = self._call_note_params(True, call_from_label)
            ok = await self._add_call_note("leads", lead_id, phone, is_incoming, 0, False, None, call_time, cr, cs)
            return ProcessCallOutcome(
                success=ok,
                upload_status="uploaded" if ok else "failed",
                lead_id=lead_id,
                reason=None if ok else "Missed call note failed",
            )

        download_link = await self._attach_audio("leads", lead_id, audio_path)
        if has_recording and not download_link:
            # No note without audio: fail the job so a retry attaches the recording
            # instead of leaving an empty card in Kommo.
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                lead_id=lead_id,
                reason="Recording upload to Kommo Drive failed",
            )
        if call_result:
            cr, cs = call_result, call_status
        else:
            cr, cs = self._call_note_params(False, call_from_label)
        ok = await self._add_call_note(
            "leads",
            lead_id,
            phone,
            is_incoming,
            duration_seconds,
            was_answered,
            download_link,
            call_time,
            cr,
            cs,
        )
        return ProcessCallOutcome(
            success=ok,
            upload_status="uploaded" if ok else "failed",
            lead_id=lead_id,
            reason=None if ok else "Failed to add call note",
            upload_source=upload_source if download_link else None,
        )

    async def _process_for_contact(
        self,
        contact_id: int,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: datetime,
        call_from_label: Optional[str],
        upload_source: Optional[str],
        call_result: Optional[str] = None,
        call_status: Optional[int] = None,
    ) -> ProcessCallOutcome:
        has_recording = bool(audio_path and Path(audio_path).is_file() and Path(audio_path).stat().st_size > 0)
        is_missed = not was_answered and not has_recording

        if is_missed:
            if call_result:
                cr, cs = call_result, call_status
            else:
                cr, cs = self._call_note_params(True, call_from_label)
            ok = await self._add_call_note("contacts", contact_id, phone, is_incoming, 0, False, None, call_time, cr, cs)
            return ProcessCallOutcome(
                success=ok,
                upload_status="uploaded" if ok else "failed",
                contact_id=contact_id,
                reason=None if ok else "Missed call note failed",
            )

        download_link = await self._attach_audio("contacts", contact_id, audio_path)
        if has_recording and not download_link:
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                contact_id=contact_id,
                reason="Recording upload to Kommo Drive failed",
            )
        if call_result:
            cr, cs = call_result, call_status
        else:
            cr, cs = self._call_note_params(False, call_from_label)
        ok = await self._add_call_note(
            "contacts",
            contact_id,
            phone,
            is_incoming,
            duration_seconds,
            was_answered,
            download_link,
            call_time,
            cr,
            cs,
        )
        return ProcessCallOutcome(
            success=ok,
            upload_status="uploaded" if ok else "failed",
            contact_id=contact_id,
            reason=None if ok else "Failed to add call note",
            upload_source=upload_source if download_link else None,
        )

    async def _attach_audio(self, entity: str, entity_id: int, audio_path: Optional[str]) -> Optional[str]:
        if not audio_path or not Path(audio_path).is_file():
            print("[kommo_crm] attach_audio: file missing", flush=True)
            return None
        file_uuid = await self._upload_file(audio_path)
        if not file_uuid:
            print("[kommo_crm] attach_audio: drive upload returned no uuid", flush=True)
            return None
        # Desktop order: upload → download link → attach (attach is best-effort).
        download_link = await self._get_download_link(file_uuid)
        if not download_link:
            print(f"[kommo_crm] attach_audio: no download link for uuid={file_uuid}", flush=True)
            return None
        resp = await self._request(
            "PUT",
            f"/{entity}/{entity_id}/files",
            json_body=[{"file_uuid": file_uuid}],
        )
        if resp.status_code not in (200, 201, 202, 204):
            log.warning("attach files failed: %s %s", resp.status_code, resp.text[:200])
            print(
                f"[kommo_crm] attach files HTTP {resp.status_code} (call note link still used)",
                flush=True,
            )
        else:
            print(f"[kommo_crm] attach files ok entity={entity}/{entity_id}", flush=True)
        return download_link

    async def _upload_drive_part(self, url: str, chunk: bytes) -> Optional[dict]:
        """Upload one Drive chunk with retries (matches desktop AmoCrmService)."""
        headers: dict[str, str] = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(chunk)),
        }
        # Presigned storage URLs break if we add Authorization (Kommo direct URLs need it).
        if "amocrm" in url or "kommo" in url:
            headers["Authorization"] = f"Bearer {self.access_token}"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._upload_http.post(url, content=chunk, headers=headers)
                # Kommo Drive returns 202 for intermediate parts (desktop uses IsSuccessStatusCode).
                if resp.status_code in (200, 201, 202):
                    return resp.json()
                log.warning(
                    "drive part upload HTTP %s (attempt %s/%s): %s",
                    resp.status_code,
                    attempt + 1,
                    max_retries,
                    resp.text[:200],
                )
                print(
                    f"[kommo_crm] drive part HTTP {resp.status_code} attempt {attempt + 1}",
                    flush=True,
                )
            except (
                httpx.RemoteProtocolError,
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.NetworkError,
            ) as exc:
                log.warning(
                    "drive part upload error (attempt %s/%s): %s",
                    attempt + 1,
                    max_retries,
                    exc,
                )
                print(
                    f"[kommo_crm] drive part error attempt {attempt + 1}: {exc}",
                    flush=True,
                )
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
        return None

    async def _upload_file(self, file_path: str) -> Optional[str]:
        if not self._drive_url:
            await self.load_account_info()
        if not self._drive_url:
            return None
        path = Path(file_path)
        file_size = path.stat().st_size
        if file_size == 0:
            return None
        ext = path.suffix.lower()
        content_type = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".webm": "audio/webm",
        }.get(ext, mimetypes.guess_type(path.name)[0] or "audio/mpeg")

        session_resp = await self._request(
            "POST",
            f"{self._drive_url}/v1.0/sessions",
            json_body={
                "file_name": path.name,
                "file_size": file_size,
                "content_type": content_type,
            },
        )
        if session_resp.status_code != 200:
            log.warning("drive session failed: %s", session_resp.text[:200])
            print(
                f"[kommo_crm] drive session HTTP {session_resp.status_code}: {session_resp.text[:120]}",
                flush=True,
            )
            return None
        session = session_resp.json()
        upload_url = session.get("upload_url")
        max_part = int(session.get("max_part_size") or 524288)
        if not upload_url:
            return None

        print(
            f"[kommo_crm] drive upload start {path.name} size={file_size} parts~{(file_size + max_part - 1) // max_part}",
            flush=True,
        )

        uploaded = 0
        current_url = upload_url
        with path.open("rb") as fh:
            while uploaded < file_size:
                chunk = fh.read(min(max_part, file_size - uploaded))
                if not chunk:
                    break
                data = await self._upload_drive_part(current_url, chunk)
                if not data:
                    log.warning("drive part upload failed after retries at offset %s", uploaded)
                    return None
                uploaded += len(chunk)
                if uploaded < file_size:
                    current_url = data.get("next_url")
                    if not current_url:
                        log.warning("drive next_url missing after part upload")
                        return None
                else:
                    file_uuid = data.get("uuid") or data.get("file_uuid")
                    if file_uuid:
                        print(f"[kommo_crm] drive upload ok uuid={file_uuid}", flush=True)
                        return str(file_uuid)
                    log.warning("drive upload finished without uuid: %s", data)
                    return None
        return None

    async def _get_download_link(self, file_uuid: str) -> Optional[str]:
        if not self._drive_url:
            await self.load_account_info()
        if not self._drive_url:
            return None
        resp = await self._request("GET", f"{self._drive_url}/v1.0/files/{file_uuid}")
        if resp.status_code == 401 and self._token_refresher:
            await self._ensure_token(force_refresh=True)
            resp = await self._request("GET", f"{self._drive_url}/v1.0/files/{file_uuid}")
        if resp.status_code != 200:
            log.warning("drive file info HTTP %s: %s", resp.status_code, resp.text[:200])
            print(
                f"[kommo_crm] drive file info HTTP {resp.status_code}: {resp.text[:120]}",
                flush=True,
            )
            return None
        data = resp.json()
        link = ((data.get("_links") or {}).get("download") or {}).get("href")
        if not link:
            link = (data.get("download") or {}).get("href")
        if not link:
            link = data.get("href")
        if link:
            print(f"[kommo_crm] drive download link ok uuid={file_uuid}", flush=True)
        else:
            log.warning("drive download link missing in response: %s", str(data)[:300])
            print(f"[kommo_crm] drive download link missing for uuid={file_uuid}", flush=True)
        return link

    async def list_pipelines(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/leads/pipelines", params={"with": "statuses"})
        if resp.status_code != 200:
            log.warning("list pipelines failed: %s", resp.status_code)
            return []
        pipelines = (resp.json().get("_embedded") or {}).get("pipelines") or []
        out: list[dict[str, Any]] = []
        for pipe in pipelines:
            pid = pipe.get("id")
            if not pid:
                continue
            statuses = []
            for st in ((pipe.get("_embedded") or {}).get("statuses")) or []:
                sid = st.get("id")
                if sid:
                    statuses.append({"id": int(sid), "name": st.get("name") or f"Stage #{sid}"})
            out.append(
                {
                    "id": int(pid),
                    "name": pipe.get("name") or f"Pipeline #{pid}",
                    "statuses": statuses,
                }
            )
        return out

    async def _fetch_entity_tags_paginated(
        self,
        entity_type: str,
        *,
        query: str = "",
        max_pages: int = 1,
    ) -> list[dict[str, Any]]:
        """Load tag pages for leads/contacts (Kommo limit 250/page)."""
        out: list[dict[str, Any]] = []
        q = (query or "").strip()
        page_limit = max(1, min(int(max_pages or 1), 40))
        page = 1
        while page <= page_limit:
            params: dict[str, Any] = {"page": page, "limit": 250}
            if q:
                params["query"] = q
            resp = await self._request("GET", f"/{entity_type}/tags", params=params)
            if resp.status_code != 200:
                log.warning(
                    "list %s tags failed on page %s: %s %s",
                    entity_type,
                    page,
                    resp.status_code,
                    resp.text[:200],
                )
                break
            data = resp.json()
            tags = (data.get("_embedded") or {}).get("tags") or []
            if not tags:
                break
            for tag in tags:
                tid = tag.get("id")
                if not tid:
                    continue
                name = tag.get("name") or f"Tag #{tid}"
                if is_noise_lead_tag(name):
                    continue
                out.append(
                    {
                        "id": int(tid),
                        "name": name,
                        "color": tag.get("color"),
                        "source": entity_type,
                    }
                )
            if len(tags) < 250:
                break
            links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
            if not links.get("next"):
                break
            page += 1
        return out

    async def list_lead_tags(self, *, query: str = "") -> list[dict[str, Any]]:
        """Tags for admin rule picker: lead tags + contact-only tags (by name)."""
        q = (query or "").strip()
        # Full catalog scan is too slow (rate limit + thousands of tags). Search loads more pages.
        max_pages = 8 if len(q) >= 2 else 1
        lead_tags = await self._fetch_entity_tags_paginated(
            "leads", query=q, max_pages=max_pages
        )
        contact_tags = await self._fetch_entity_tags_paginated(
            "contacts", query=q, max_pages=max_pages
        )
        by_name: dict[str, dict[str, Any]] = {}
        for tag in lead_tags:
            key = (tag.get("name") or "").strip().lower()
            if not key:
                continue
            by_name[key] = {
                "id": tag["id"],
                "name": tag["name"],
                "color": tag.get("color"),
                "source": "leads",
            }
        for tag in contact_tags:
            key = (tag.get("name") or "").strip().lower()
            if not key or key in by_name:
                continue
            by_name[key] = {
                "id": tag["id"],
                "name": tag["name"],
                "color": tag.get("color"),
                "source": "contacts",
            }
        out = list(by_name.values())
        out.sort(key=lambda t: (t.get("name") or "").lower())
        return out

    async def get_contact_responsible_user_id(self, contact_id: int) -> Optional[int]:
        contact = await self._get_contact(contact_id)
        if not contact:
            return None
        uid = contact.get("responsible_user_id")
        try:
            return int(uid) if uid is not None else None
        except (TypeError, ValueError):
            return None

    async def get_lead_responsible_user_id(self, lead_id: int) -> Optional[int]:
        lead = await self.get_lead(lead_id)
        if not lead:
            return None
        uid = lead.get("responsible_user_id")
        try:
            return int(uid) if uid is not None else None
        except (TypeError, ValueError):
            return None

    def _phone_custom_field(self, phone: str) -> list[dict[str, Any]]:
        return [
            {
                "field_code": "PHONE",
                "values": [{"value": phone, "enum_code": "WORK"}],
            }
        ]

    async def create_contact_and_lead(
        self,
        phone: str,
        *,
        contact_name: str,
        lead_name: str,
        pipeline_id: Any = None,
        status_id: Any = None,
        responsible_user_id: Optional[int] = None,
        tag_id: Any = None,
        tag_name: Optional[str] = None,
    ) -> Optional[dict[str, int]]:
        user_id = responsible_user_id or self.get_acting_user_id()
        if not user_id:
            log.warning("create_contact_and_lead: no responsible user")
            return None
        item: dict[str, Any] = {
            "name": lead_name,
            "responsible_user_id": int(user_id),
            "created_by": int(user_id),
            "_embedded": {
                "contacts": [
                    {
                        "name": contact_name,
                        "responsible_user_id": int(user_id),
                        "created_by": int(user_id),
                        "custom_fields_values": self._phone_custom_field(phone),
                    }
                ]
            },
        }
        try:
            if pipeline_id:
                item["pipeline_id"] = int(pipeline_id)
            if status_id:
                item["status_id"] = int(status_id)
        except (TypeError, ValueError):
            pass
        tag_entry: Optional[dict[str, Any]] = None
        try:
            if tag_id:
                n = int(tag_id)
                if n > 0:
                    tag_entry = {"id": n}
        except (TypeError, ValueError):
            tag_entry = None
        if tag_entry is None:
            name = (tag_name or "").strip()
            if name:
                tag_entry = {"name": name}
        if tag_entry:
            item["tags_to_add"] = [tag_entry]
        resp = await self._request("POST", "/leads/complex", json_body=[item])
        if resp.status_code not in (200, 201):
            log.warning(
                "create_contact_and_lead failed: %s %s",
                resp.status_code,
                resp.text[:300],
            )
            return None
        data = resp.json()
        leads: list[Any] = []
        if isinstance(data, list):
            leads = data
        elif isinstance(data, dict):
            embedded = data.get("_embedded")
            if isinstance(embedded, dict):
                raw = embedded.get("leads")
                if isinstance(raw, list):
                    leads = raw
            if not leads and data.get("id"):
                leads = [data]
        if not leads:
            log.warning("create_contact_and_lead: unexpected response shape: %s", str(data)[:300])
            return None
        lead = leads[0] if isinstance(leads[0], dict) else {}
        lead_id = lead.get("id")
        contact_id = None
        embedded = lead.get("_embedded") if isinstance(lead, dict) else None
        contacts = []
        if isinstance(embedded, dict):
            raw_contacts = embedded.get("contacts")
            if isinstance(raw_contacts, list):
                contacts = raw_contacts
        if contacts and isinstance(contacts[0], dict) and contacts[0].get("id"):
            contact_id = int(contacts[0]["id"])
        if not contact_id:
            contact_id = await self.find_contact_by_phone(phone)
        if not lead_id:
            return None
        print(
            f"[kommo_crm] created contact={contact_id} lead={lead_id} for {phone}"
            + (f" tag={tag_entry}" if tag_entry else ""),
            flush=True,
        )
        return {
            "contact_id": int(contact_id) if contact_id else None,
            "lead_id": int(lead_id),
        }

    async def create_task(
        self,
        text: str,
        *,
        entity_type: str,
        entity_id: int,
        responsible_user_id: int,
        complete_till: int,
    ) -> Optional[int]:
        body = [
            {
                "text": text,
                "entity_type": entity_type,
                "entity_id": int(entity_id),
                "responsible_user_id": int(responsible_user_id),
                "complete_till": int(complete_till),
            }
        ]
        resp = await self._request("POST", "/tasks", json_body=body)
        if resp.status_code in (200, 201):
            task_id: Optional[int] = None
            try:
                tasks = (resp.json().get("_embedded") or {}).get("tasks") or []
                if tasks and tasks[0].get("id") is not None:
                    task_id = int(tasks[0]["id"])
            except (TypeError, ValueError, AttributeError):
                task_id = None
            print(
                f"[kommo_crm] task created on {entity_type}/{entity_id} id={task_id or '?'}: {text[:80]}",
                flush=True,
            )
            return task_id if task_id is not None else 0
        log.warning("create task failed: %s %s", resp.status_code, resp.text[:300])
        return None

    async def _add_call_note(
        self,
        entity: str,
        entity_id: int,
        phone: str,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_link: Optional[str],
        call_time: datetime,
        call_result: Optional[str],
        call_status: Optional[int],
    ) -> bool:
        user_id = self.get_acting_user_id()
        if not user_id:
            log.warning("no acting kommo user id for call note")
            return False

        direction = "in" if is_incoming else "out"
        normalized = self.normalize_phone(phone)
        uniq_src = f"{entity}_{entity_id}_{direction}_{normalized}_{call_time.strftime('%Y%m%d%H%M%S')}"
        uniq = hashlib.sha256(uniq_src.encode()).hexdigest()[:16]
        note_type = "call_in" if is_incoming else "call_out"

        for try_result in (bool(call_result), False):
            params: dict[str, Any] = {
                "uniq": uniq,
                "duration": duration_seconds,
                "source": "Callspire",
                "phone": phone,
            }
            if audio_link:
                params["link"] = audio_link
            if try_result and call_result:
                params["call_result"] = call_result
                if call_status is not None:
                    params["call_status"] = call_status

            body = [
                {
                    "note_type": note_type,
                    "created_by": user_id,
                    "responsible_user_id": user_id,
                    "params": params,
                }
            ]
            resp = await self._request("POST", f"/{entity}/{entity_id}/notes", json_body=body)
            if resp.status_code in (200, 201):
                return True
            if try_result and resp.status_code == 400:
                continue
            log.warning("call note failed: %s %s", resp.status_code, resp.text[:300])
            return False
        return False
