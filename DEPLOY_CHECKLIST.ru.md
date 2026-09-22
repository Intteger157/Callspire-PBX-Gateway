# Деплой в `/opt/callspire-pbx-gateway/` (корень gateway)

На сервере **уже есть** `kommo_oauth.py`, `kommo_service.py`, старый `app_kommo.py`.  
Ниже — что копировать из `gateway-patch` / `pbx-gateway-kommo-bundle` в **корень**.

## Копировать в корень (да)

| Файл | Действие |
|------|----------|
| `kommo_crm.py` | **новый** — положить в корень |
| `kommo_recording.py` | **новый** |
| `kommo_call_worker.py` | **новый** |
| `kommo_jobs_db.py` | **новый** |
| `kommo_store.py` | **новый** |
| `app_kommo.py` | **заменить** старый (сначала бэкап: `app_kommo.py.bak`) |
| `templates/admin_kommo.html` | в `templates/` (можно рядом с существующими) |

## Не удалять на сервере

| Файл | Зачем оставить |
|------|----------------|
| `kommo_oauth.py` | OAuth может храниться через `permissions_db` — не трогать |
| `kommo_service.py` | старая логика; новый upload идёт через `kommo_crm.py`, файл не мешает |
| `permissions_db.py`, `permissions.db` | токены и mapping extension→user |

## `app.py` — не заменять целиком

На сервере свой `app.py` (66 KB). **Не перезаписывайте** файлом из patch (68 KB), если не уверены.

Внесите только 3 правки из [`APP_PY_PATCH.md`](APP_PY_PATCH.md):

1. `import shutil`
2. функции `_kommo_internal_query_cdr` и `_kommo_internal_download_recording`
3. в `register_kommo_routes(...)` добавить аргументы:
   - `query_cdr=_kommo_internal_query_cdr`
   - `download_recording=_kommo_internal_download_recording`
   - `extension_from_token=_extension_from_token`

## Не копировать из patch

- `INTEGRATION_APP_EXAMPLE.py`
- `requirements-web-softphone.txt` (у вас уже есть `requirements.txt`)
- `README_KOMMO_BUNDLE.md` — только справка

## После копирования

```bash
cd /opt/callspire-pbx-gateway
source venv/bin/activate
pip install httpx   # обязательно для kommo_crm / app_kommo
python -c "import app_kommo, kommo_crm, kommo_recording, kommo_call_worker, kommo_jobs_db, kommo_store; print('OK')"
sudo systemctl restart callspire-pbx-gateway   # или ваше имя сервиса
```

## Сервис не стартует (exit-code 1)

Сначала смотрим traceback:

```bash
sudo journalctl -u callspire-pbx-gateway -n 80 --no-pager
cd /opt/callspire-pbx-gateway
source venv/bin/activate
python app.py 2>&1 | head -40
```

Типичные ошибки и исправления:

| Ошибка в логе | Что сделать |
|---------------|-------------|
| `ModuleNotFoundError: No module named 'httpx'` | `pip install httpx` в venv, restart |
| `ModuleNotFoundError: No module named 'kommo_crm'` (или другой `kommo_*`) | Скопировать все 5 новых `kommo_*.py` в корень gateway |
| `TypeError: register_kommo_routes() got an unexpected keyword argument` | Обновить `app_kommo.py` из bundle (в новой версии лишние kwargs игнорируются) |
| `NameError: _kommo_internal_query_cdr` | В `app.py` не применён патч из `APP_PY_PATCH.md` (п. 2–3) |
| `SyntaxError` в `app.py` | Откат: `cp app.py.bak app.py`, применить патч вручную |

Быстрый откат:

```bash
cd /opt/callspire-pbx-gateway
cp app.py.bak app.py          # если делали бэкап
cp app_kommo.py.bak app_kommo.py
sudo systemctl restart callspire-pbx-gateway
```

Проверка:

```bash
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:PORT/api/kommo/status
```

В десктопе: **Settings → Kommo → Recording upload source → PBX Gateway**.

## Итоговая структура корня (фрагмент)

```
/opt/callspire-pbx-gateway/
  app.py                          ← только патч
  app_kommo.py                    ← НОВЫЙ (process-call API)
  kommo_crm.py                    ← НОВЫЙ
  kommo_recording.py              ← НОВЫЙ
  kommo_call_worker.py            ← НОВЫЙ
  kommo_jobs_db.py                ← НОВЫЙ
  kommo_store.py                  ← НОВЫЙ
  kommo_oauth.py                  ← оставить как было
  kommo_service.py                ← оставить как было
  permissions_db.py
  templates/admin_kommo.html      ← можно добавить
```

## Web softphone: login «API returned HTML instead of JSON»

**Причина:** на сервере старый `mount.py` (браузерный API в `/api/*`), а новый `dist` ходит в `/softphone/api/*`. Запрос попадает в SPA catch-all → `index.html` вместо JSON.

### Скопировать

| Откуда (репо) | Куда на сервере |
|---------------|-----------------|
| `deploy/web-softphone/gateway-web-softphone/gateway_web_softphone/mount.py` | `/opt/callspire-pbx-gateway/gateway-web-softphone/gateway_web_softphone/mount.py` |
| `callspire-web-softphone/softphone-web/dist/*` | каталог статики softphone (как в `SOFTPHONE_STATIC_DIR` / `static/softphone/`) |

```bash
sudo systemctl restart callspire-pbx-gateway
```

### Проверка (после рестарта)

```bash
# В openapi должны быть /softphone/api/auth/login, /softphone/api/cdr, …
curl -s http://127.0.0.1:8005/openapi.json | grep -o '"/softphone/api[^"]*"' | head

# health = JSON {"ok":true}, не HTML
curl -s http://127.0.0.1:8005/softphone/api/health

# без сессии = 401 JSON, НЕ 200 HTML
curl -s -o /dev/null -w "cdr %{http_code}\n" http://127.0.0.1:8005/softphone/api/cdr
curl -s -o /dev/null -w "login POST " http://127.0.0.1:8005/softphone/api/auth/login \
  -X POST -H "Content-Type: application/json" -d '{"email":"x","password":"y"}'

# desktop JWT по-прежнему на /api/*
curl -s -o /dev/null -w "legacy cdr %{http_code}\n" http://127.0.0.1:8005/api/cdr
```

Ожидание:

| URL | Код | Тело |
|-----|-----|------|
| `/softphone/api/health` | 200 | `{"ok":true}` |
| `/softphone/api/cdr` | 401 | JSON `Not authenticated` |
| `/softphone/api/auth/login` (bad creds) | 401/422 | JSON |
| `/api/cdr` | 401 | JSON (desktop) |

Если `/softphone/api/cdr` даёт **200** и `curl` показывает `<!doctype html>` — `mount.py` ещё старый или сервис не перезапущен.

### Все `/softphone/api/*` → 500 после логина

**Причина (частая):** вложенный ASGI-запрос (`httpx.ASGITransport`) снова проходит через `SessionMiddleware` / `BaseHTTPMiddleware` → 500 на всех проксируемых маршрутах.

**Исправление:** обновить `mount.py` из репо (версия с `app.state._callspire_internal_asgi` и `{{job_id}}` в путях Kommo).

**Диагностика на сервере:**

```bash
cd /opt/callspire-pbx-gateway
source venv/bin/activate

# логин + cookie
curl -s -c /tmp/sid.jar -X POST http://127.0.0.1:8005/softphone/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"USER","password":"PASS"}'

# должно быть JSON, не 500
curl -s -b /tmp/sid.jar http://127.0.0.1:8005/softphone/api/my-callerids | head -c 200
curl -s -b /tmp/sid.jar 'http://127.0.0.1:8005/softphone/api/cdr?limit=5' | head -c 200

# traceback в journal
sudo journalctl -u callspire-pbx-gateway -n 50 --no-pager
```

Если `my-callerids` с cookie всё ещё 500 — смотрите traceback: часто это `app.py` без `/api/my-callerids` / `/api/cdr` (нужен полный `gateway-patch/app.py`) или ошибка `permissions_db` / CDR path.
