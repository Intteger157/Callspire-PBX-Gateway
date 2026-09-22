"""Kommo entity-creation rules by call type (Miko amoCRM integration style)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import permissions_db
import kommo_jobs_db

log = logging.getLogger("kommo_entity_rules")


def _is_cdr_entity_processed(linkedid: str) -> bool:
    fn = getattr(kommo_jobs_db, "is_cdr_entity_processed", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(linkedid))
    except Exception as exc:
        log.warning("is_cdr_entity_processed failed: %s", exc)
        return False


def _mark_cdr_entity_processed(linkedid: str, **kwargs: Any) -> None:
    fn = getattr(kommo_jobs_db, "mark_cdr_entity_processed", None)
    if not callable(fn):
        return
    try:
        fn(linkedid, **kwargs)
    except Exception as exc:
        log.warning("mark_cdr_entity_processed failed: %s", exc)

# Call types aligned with MikoPBX amoCRM module.
CALL_TYPES: dict[str, str] = {
    "failed_outgoing_known": "Failed outgoing to known number",
    "failed_outgoing_unknown": "Failed outgoing to unknown number",
    "answered_incoming_known": "Answered incoming from known number",
    "answered_incoming_unknown": "Answered incoming from unknown number",
    "answered_outgoing_known": "Answered outgoing to known number",
    "answered_outgoing_unknown": "Answered outgoing to unknown number",
    "missed_incoming_known": "Missed from known number",
    "missed_incoming_unknown": "Missed from unknown number",
}

ENTITY_ACTIONS: dict[str, str] = {
    "none": "Do nothing",
    "create_contact_and_lead": "Create contact and deal",
}

TASK_RESPONSIBLE_MODES: dict[str, str] = {
    "client_owner": "Contact/deal owner",
    "first_answered": "First who answered the call",
    "specific": "Specific user",
}


@dataclass
class EntityRuleResult:
    contact_id: Optional[int] = None
    lead_id: Optional[int] = None
    task_created: bool = False
    task_id: Optional[int] = None
    rule_id: Optional[int] = None
    call_type: Optional[str] = None


def classify_call_type(
    *,
    is_incoming: bool,
    was_answered: bool,
    contact_known: bool,
) -> str:
    known = "known" if contact_known else "unknown"
    if is_incoming:
        if was_answered:
            return f"answered_incoming_{known}"
        return f"missed_incoming_{known}"
    if was_answered:
        return f"answered_outgoing_{known}"
    return f"failed_outgoing_{known}"


def normalize_did(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def render_template(
    template: str,
    *,
    phone: str,
    did: str = "",
    call_from_label: str = "",
) -> str:
    text = (template or "").strip()
    if not text:
        return ""
    repl = {
        "<PhoneNumber>": phone,
        "<DID>": did,
        "<CallFromLabel>": call_from_label,
    }
    for key, val in repl.items():
        text = text.replace(key, val or "")
    return text.strip()


def find_matching_rule(
    rules: list[dict[str, Any]],
    call_type: str,
    did: str = "",
) -> Optional[dict[str, Any]]:
    did_norm = normalize_did(did)
    enabled = [r for r in rules if r.get("enabled") and r.get("call_type") == call_type]
    if not enabled:
        return None
    for rule in enabled:
        rule_did = normalize_did(rule.get("did") or "")
        if rule_did and rule_did == did_norm:
            return rule
    for rule in enabled:
        if not (rule.get("did") or "").strip():
            return rule
    return None


def list_rules() -> list[dict[str, Any]]:
    return permissions_db.list_kommo_entity_rules()


async def apply_entity_rule(
    client: Any,
    *,
    phone: str,
    is_incoming: bool,
    was_answered: bool,
    call_time: datetime,
    did: str = "",
    call_from_label: str = "",
    acting_user_id: Optional[int] = None,
    explicit_lead_id: Optional[int] = None,
    linkedid: Optional[str] = None,
    entity_source: str = "process_call",
) -> EntityRuleResult:
    """Apply configured entity rule before call-note upload."""
    result = EntityRuleResult()
    lid = (linkedid or "").strip()
    if lid and _is_cdr_entity_processed(lid):
        print(
            f"[kommo_entity_rules] skip linkedid={lid}: entity rules already applied",
            flush=True,
        )
        contact_id = await client.find_contact_by_phone(phone)
        if explicit_lead_id:
            result.lead_id = explicit_lead_id
        elif contact_id:
            result.contact_id = contact_id
            result.lead_id = await client.find_open_lead_for_contact(contact_id)
        return result

    contact_id = await client.find_contact_by_phone(phone)
    contact_known = contact_id is not None
    call_type = classify_call_type(
        is_incoming=is_incoming,
        was_answered=was_answered,
        contact_known=contact_known,
    )
    result.call_type = call_type

    rule = find_matching_rule(list_rules(), call_type, did)
    if not rule:
        msg = (
            f"[kommo_entity_rules] no rule for call_type={call_type} did={did or '-'} "
            f"contact_known={contact_known} phone={phone}"
        )
        log.info(msg)
        print(msg, flush=True)
        if explicit_lead_id:
            result.lead_id = explicit_lead_id
        elif contact_id:
            result.contact_id = contact_id
            result.lead_id = await client.find_open_lead_for_contact(contact_id)
        return result

    result.rule_id = int(rule["id"])
    print(
        f"[kommo_entity_rules] matched rule id={rule['id']} call_type={call_type} "
        f"did={did or '-'} create_task={bool(rule.get('create_task'))} "
        f"entity_action={rule.get('entity_action')}",
        flush=True,
    )
    responsible = _resolve_responsible_user_id(rule, acting_user_id)

    entity_action = (rule.get("entity_action") or "none").strip()
    if entity_action == "create_contact_and_lead" and not contact_known and not explicit_lead_id:
        contact_name = render_template(
            rule.get("contact_name_template") or "New contact <PhoneNumber>",
            phone=phone,
            did=did,
            call_from_label=call_from_label,
        )
        lead_name = render_template(
            rule.get("lead_name_template") or "New deal <PhoneNumber>",
            phone=phone,
            did=did,
            call_from_label=call_from_label,
        )
        created = await client.create_contact_and_lead(
            phone,
            contact_name=contact_name or f"Contact {phone}",
            lead_name=lead_name or f"Deal {phone}",
            pipeline_id=rule.get("pipeline_id"),
            status_id=rule.get("status_id"),
            responsible_user_id=responsible,
            tag_id=rule.get("lead_tag_id"),
            tag_name=rule.get("lead_tag_name"),
        )
        if created:
            result.contact_id = created.get("contact_id")
            result.lead_id = created.get("lead_id")
            contact_id = result.contact_id
            contact_known = contact_id is not None

    if explicit_lead_id:
        result.lead_id = explicit_lead_id
    elif not result.lead_id and contact_id:
        result.contact_id = contact_id
        result.lead_id = await client.find_open_lead_for_contact(contact_id)
    elif result.contact_id and not result.lead_id:
        result.lead_id = await client.find_open_lead_for_contact(result.contact_id)

    if rule.get("create_task"):
        task_text = render_template(
            rule.get("task_name_template") or "Call back <PhoneNumber>",
            phone=phone,
            did=did,
            call_from_label=call_from_label,
        )
        if task_text:
            task_user = await _resolve_task_responsible(
                client,
                rule,
                contact_id=result.contact_id or contact_id,
                lead_id=result.lead_id,
                acting_user_id=acting_user_id,
                default_responsible=responsible,
            )
            deadline_hours = int(rule.get("task_deadline_hours") or 0)
            complete_till = int(
                (call_time + timedelta(hours=max(0, deadline_hours))).timestamp()
            )
            entity_type, entity_id = _task_entity(result.lead_id, result.contact_id or contact_id)
            if entity_type and entity_id and task_user:
                task_id = await client.create_task(
                    task_text,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    responsible_user_id=task_user,
                    complete_till=complete_till,
                )
                result.task_created = task_id is not None
                result.task_id = task_id
                print(
                    f"[kommo_entity_rules] task {'created' if result.task_created else 'FAILED'} "
                    f"rule={rule['id']} {entity_type}/{entity_id} user={task_user} "
                    f"task_id={task_id or '-'} text={task_text[:80]!r}",
                    flush=True,
                )
            else:
                msg = (
                    f"[kommo_entity_rules] task skipped rule={rule['id']}: "
                    f"entity={entity_type}/{entity_id} task_user={task_user} "
                    f"contact_id={result.contact_id or contact_id} lead_id={result.lead_id}"
                )
                log.warning(msg)
                print(msg, flush=True)
        else:
            print(
                f"[kommo_entity_rules] task skipped rule={rule['id']}: empty task template",
                flush=True,
            )

    if lid and result.rule_id:
        _mark_cdr_entity_processed(
            lid,
            phone=phone,
            source=entity_source,
            rule_id=result.rule_id,
            task_created=result.task_created,
            task_id=result.task_id,
            contact_id=result.contact_id,
            lead_id=result.lead_id,
        )

    return result


def _resolve_responsible_user_id(rule: dict[str, Any], acting_user_id: Optional[int]) -> Optional[int]:
    uid = rule.get("default_responsible_user_id")
    if uid:
        try:
            n = int(uid)
            return n if n > 0 else acting_user_id
        except (TypeError, ValueError):
            pass
    return acting_user_id


async def _resolve_task_responsible(
    client: Any,
    rule: dict[str, Any],
    *,
    contact_id: Optional[int],
    lead_id: Optional[int],
    acting_user_id: Optional[int],
    default_responsible: Optional[int],
) -> Optional[int]:
    mode = (rule.get("task_responsible") or "client_owner").strip()
    if mode == "specific":
        uid = rule.get("task_responsible_user_id")
        try:
            n = int(uid) if uid is not None else 0
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    if mode == "first_answered" and acting_user_id:
        return acting_user_id
    if lead_id:
        owner = await client.get_lead_responsible_user_id(lead_id)
        if owner:
            return owner
    if contact_id:
        owner = await client.get_contact_responsible_user_id(contact_id)
        if owner:
            return owner
    return default_responsible or acting_user_id or client.get_acting_user_id()


def _task_entity(
    lead_id: Optional[int], contact_id: Optional[int]
) -> tuple[Optional[str], Optional[int]]:
    if lead_id:
        return "leads", int(lead_id)
    if contact_id:
        return "contacts", int(contact_id)
    return None, None
