# Патч для `app.py` на сервере PBX Gateway

Применяйте, если **не** копируете полный `app.py` из `deploy/web-softphone/gateway-patch/app.py`.

## 1. Import

Добавьте `import shutil` к остальным import в начале файла:

```python
import shutil
```

## 2. Internal helpers (перед `# ======================= CallerID / Originate =======================`)

Вставьте после функции `_recording_filename_hint`:

```python
async def _kommo_internal_query_cdr(
    *,
    ext: str,
    dst: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """CDR rows for Kommo worker (no JWT — extension already validated)."""
    if _rest_enabled():
        trunk_cids = await load_trunk_callerids_rest(miko_rest)
        return await query_cdr_rest(
            miko_rest,
            trunk_cids,
            ext=ext,
            dst=dst,
            start_from=start_from,
            start_to=start_to,
            limit=limit,
            offset=offset,
        )
    return query_cdr(
        cdr_db_path=cfg["cdr_db_path"],
        config_db_path=cfg["config_db_path"],
        ext=ext,
        dst=dst,
        start_from=start_from,
        start_to=start_to,
        limit=limit,
        offset=offset,
        **_cdr_docker_kwargs(),
    )


async def _kommo_internal_download_recording(linkedid: str, dest: Path) -> bool:
    """Download Miko recording to dest path for Kommo upload."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _rest_enabled():
        try:
            cdr_row = await find_cdr_by_linkedid_rest(miko_rest, linkedid)
        except MikoRestError:
            return False
        if cdr_row is None or not (cdr_row.get("playback_url") or "").strip():
            return False
        try:
            upstream = await miko_rest.stream_cdr_playback(cdr_row["playback_url"])
        except MikoRestError:
            return False
        try:
            with dest.open("wb") as out:
                async for chunk in upstream.aiter_bytes():
                    out.write(chunk)
            return dest.is_file() and dest.stat().st_size > 0
        finally:
            await upstream.aclose()

    try:
        file_path = find_recording_path(
            cdr_db_path=cfg["cdr_db_path"],
            recording_base=cfg.get("recording_base", "/var/spool/mikopbx"),
            linkedid=linkedid,
            **_cdr_docker_kwargs(),
        )
    except Exception:
        return False
    if not file_path:
        return False
    src = Path(file_path)
    if not src.is_file():
        return False
    shutil.copy2(src, dest)
    return dest.is_file() and dest.stat().st_size > 0
```

## 3. `register_kommo_routes`

Замените вызов в конце `app.py` на:

```python
register_kommo_routes(
    app,
    cfg=cfg,
    require_admin=require_admin,
    require_jwt=require_jwt,
    templates=templates,
    html_context=_html_context,
    kommo_default_redirect_uri=_kommo_default_redirect_uri,
    query_cdr=_kommo_internal_query_cdr,
    download_recording=_kommo_internal_download_recording,
    extension_from_token=_extension_from_token,
)
```

## 4. Проверка

```bash
curl -s -H "Authorization: Bearer $JWT" https://your-gateway/api/kommo/status | jq .
# Ожидается JSON с полями available, offer_gateway, ...

curl -s -X POST -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"phone":"+79991234567","call_time":"2026-07-03T12:00:00+03:00","is_incoming":false,"duration_seconds":30,"was_answered":true,"client_recording_enabled":false}' \
  https://your-gateway/api/kommo/process-call | jq .
```

## 5. Hotfix: originate `'OriginateRequest' object has no attribute 'ring_extension'`

Если `POST /api/originate` падает с этой ошибкой, на сервере в `originate_call` замените строку:

```python
    ring_raw = (body.ring_extension or "").strip()
```

на:

```python
    ring_raw = (getattr(body, "ring_extension", None) or "").strip()
```

И (опционально) добавьте поле в модель:

```python
class OriginateRequest(BaseModel):
    destination: str
    callerid: str
    ring_extension: str | None = None
```

Проверка на сервере после правки:

```bash
grep -n "getattr(body, \"ring_extension\"" /opt/mikopbx-cdr-proxy/app.py
# Источник правок: callspire-pbx-gateway/app.py (не deploy/web-softphone/gateway-patch)
sudo systemctl restart mikopbx-cdr.service
```
