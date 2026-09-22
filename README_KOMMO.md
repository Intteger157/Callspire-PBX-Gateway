# PBX Gateway — Kommo upload bundle

Готовый набор файлов для деплоя **серверной загрузки записей в Kommo** (`process-call` API).

Скопируйте содержимое этой папки **одним разом** в каталог PBX Gateway на сервере (типично `/opt/mikopbx-cdr-proxy/`).

## Быстрый деплой (Linux)

```bash
# На сервере, из каталога gateway:
GW=/opt/mikopbx-cdr-proxy

# Скопировать с рабочей машины (пример):
scp -r deploy/pbx-gateway-kommo-bundle/* user@pbx:$GW/

# Или если файлы уже на сервере в /tmp/kommo-bundle:
cp /tmp/kommo-bundle/*.py $GW/
cp /tmp/kommo-bundle/templates/admin_kommo.html $GW/templates/

# Перезапуск
sudo systemctl restart mikopbx-cdr-proxy   # имя сервиса может отличаться
```

## Состав пакета

| Файл | Назначение |
|------|------------|
| `app_kommo.py` | Маршруты `/api/kommo/*`, OAuth callback, process-call API, воркеры |
| `kommo_crm.py` | Kommo API v4: контакт, лид, Drive, call notes |
| `kommo_recording.py` | CDR match + скачивание записи Miko |
| `kommo_call_worker.py` | Фоновая очередь upload jobs |
| `kommo_jobs_db.py` | SQLite таблица `kommo_call_jobs` |
| `kommo_store.py` | OAuth tokens + extension→kommo_user mapping |
| `templates/admin_kommo.html` | Простая admin-страница Kommo |
| `APP_PY_PATCH.md` | Правки для существующего `app.py` (если не заменяете целиком) |

## Правки `app.py`

Если на сервере уже есть рабочий `app.py` от Callspire Gateway — **не заменяйте его целиком**.  
Откройте [`APP_PY_PATCH.md`](APP_PY_PATCH.md) и внесите 3 блока (import `shutil`, две internal-функции, аргументы `register_kommo_routes`).

Если `app.py` у вас из `deploy/web-softphone/gateway-patch/app.py` в этом репозитории — патч уже применён там; достаточно скопировать только `*.py` из этого бандла.

## Зависимости

Дополнительных pip-пакетов не требуется (используются `httpx`, `fastapi` — уже в gateway).

При первом запуске создаются SQLite-файлы рядом с модулями:

- `kommo_jobs.sqlite` — очередь upload jobs
- `kommo_store.sqlite` — OAuth (если нет `permissions_db.get_kommo_*`)

## API (для десктопного софтфона)

- `POST /api/kommo/process-call` — создать задачу
- `GET /api/kommo/process-call/{job_id}` — статус
- `PUT /api/kommo/process-call/{job_id}/recording` — multipart WAV/MP3 от клиента
- `POST /api/kommo/process-call/retry` — ручной retry

Существующие эндпоинты без изменений: `/api/kommo/status`, `/api/kommo/session`.

## Десктоп

В настройках Kommo включите **Recording upload source → PBX Gateway** (`AmoCrmRecordingUploadSource = gateway`).

Правило аудио:

- **Запись в софтфоне включена** → клиент шлёт локальный WAV на gateway
- **Запись выключена** → gateway сам берёт запись из Miko CDR

## Web softphone

Прокси для browser mount: см. изменения в  
`deploy/web-softphone/gateway-web-softphone/gateway_web_softphone/mount.py`  
(уже в monorepo, деплоится отдельно с web-softphone).
