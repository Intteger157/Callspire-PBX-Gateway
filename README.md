# Callspire PBX Gateway

**Канонический каталог gateway** в монорепо `Softphone-crossplatform`.  
Все правки gateway, Kommo, CDR и деплой-скрипты — **только здесь** (`callspire-pbx-gateway/`).

На сервере: `/opt/callspire-pbx-gateway/` (или `/opt/mikopbx-cdr-proxy/` — тот же код).

## Что лежит в каталоге

```
callspire-pbx-gateway/
├── README.md                    ← вы здесь
├── app.py, app_kommo.py         ← FastAPI gateway + Kommo API
├── cdr_client.py, config.py     ← Miko CDR / config
├── kommo_crm.py                 ← Kommo API (заметки о звонках)
├── kommo_recording.py           ← CDR-first: запись + disposition для Amo
├── kommo_call_worker.py         ← фоновые job upload
├── kommo_jobs_db.py, kommo_store.py
├── kommo_oauth.py, kommo_service.py, permissions_db.py
├── DEPLOY_CHECKLIST.ru.md       ← что копировать на prod
├── copy-to-server.ps1           ← быстрый scp Kommo-файлов
├── deploy_kommo.ps1             ← деплой Kommo на Vultr
├── scripts/deploy-to-prod.sh    ← полный rsync gateway
├── templates/                   ← admin UI
├── gateway-web-softphone/       ← веб-софтфон (mount в app.py)
└── requirements.txt
```

**Фронт (Vue/React и т.д.)** в этом репозитории не лежит: он в проекте **softphone-web** (репо `callspire-web-softphone` или соседняя папка). Собранный UI должен оказаться в каталоге **`dist`** (часто `softphone-web/dist`).

## Конфигурация (без секретов в git)

| Файл | Назначение |
|------|------------|
| **`config.example.yaml`** | Шаблон gateway: JWT, MikoPBX paths, Kommo workers, admin user. Скопируйте в `config.yaml` и подставьте свои пути. |
| **`.env.example`** | Опциональные env для systemd/dev: `SESSION_SECRET`, `SOFTPHONE_STATIC_DIR`, WebRTC fallback. Скопируйте в `.env`. |

**Не коммитить:** `config.yaml`, `.env`, `*.sqlite`, `permissions.db` — только локальные/runtime данные.

## Шаги на сервере / в dev

### 1) Собрать веб-UI

```bash
cd /path/to/softphone-web
npm ci
npm run build
```

Получится каталог `dist` с `index.html` и `assets/`.

### 2) Установить пакет в venv gateway

Из **корня репозитория**, где лежит `app.py` (если вы скопировали туда эту папку):

```bash
cd /opt/callspire-pbx-gateway   # пример
source .venv/bin/activate
pip install -e ./gateway-web-softphone
pip install -r requirements-web-softphone.txt   # если ещё не стоят зависимости gateway
```

Если `gateway-web-softphone` лежит рядом с `app.py`, путь `./gateway-web-softphone` верный.

### 3) Переменные окружения

Минимум:

```bash
export SESSION_SECRET='длинная-случайная-строка'
export SOFTPHONE_STATIC_DIR='/path/to/softphone-web/dist'
export SESSION_SECURE=true    # за HTTPS
```

Полный список: `gateway-web-softphone/README.md`.

### 4) Подключить в `app.py`

В **самый конец** файла (после всех `include_router`):

```python
from gateway_web_softphone import install_web_softphone

install_web_softphone(app)
```

Если каталог с UI не задаётся через `SOFTPHONE_STATIC_DIR`, можно явно:

```python
install_web_softphone(app, static_dir="/opt/mikopbx-cdr-proxy/softphone-web")
```

Подсказки по env: см. **`INTEGRATION_APP_EXAMPLE.py`** (в той же папке).

### 5) Один процесс вместо BFF

- **Раньше:** nginx → Node `softphone-bff` + отдельно uvicorn gateway.  
- **Теперь:** nginx → **только** uvicorn с gateway; веб открывается по `https://домен/softphone/`.

## Kommo CRM integration

Admin UI: **Settings → Kommo CRM**. One OAuth authorization for all Callspire clients.

### Redirect URI

Register this URL in Kommo → your integration → **Redirect URI** (must match exactly):

```
https://<your-gateway-host>/<base-path>/oauth/kommo/callback
```

Example: `https://pbx.example.com/tool/oauth/kommo/callback`

The gateway auto-detects the URL from its public address. Use **Reset redirect URI** in admin if unsure, then paste the same value into Kommo.

### Domain

In admin, **Domain** accepts a full host or short name:

- `yourcompany.amocrm.ru` (RU accounts)
- `yourcompany.kommo.com` (global Kommo)
- `yourcompany` (short name → defaults to `.amocrm.ru`)

Filled automatically after **Authorize with Kommo**.

### Per-user exclusions

**PBX Users** → column **Gateway Kommo** → uncheck **Use shared** to exclude an extension from the company Kommo session (they can still use local Kommo in the desktop app).

### Deploy (Kommo + CDR)

```powershell
cd callspire-pbx-gateway
.\copy-to-server.ps1 -Server root@vultr -GatewayPath /opt/callspire-pbx-gateway
# или полный деплой:
# ./scripts/deploy-to-prod.sh root@vultr /opt/callspire-pbx-gateway
```

Kommo upload pipeline (CDR → заметка в лид без записи):

| Файл | Назначение |
|------|------------|
| `kommo_recording.py` | `resolve_pbx_call()` — CDR disposition, billsec, запись |
| `kommo_call_worker.py` | job worker, CDR-first без 8-мин ожидания |
| `kommo_crm.py` | `process_call()` → заметка в Kommo |
| `kommo_jobs_db.py` | очередь jobs |

См. **`DEPLOY_CHECKLIST.ru.md`**.

---

## Не править копии в `deploy/web-softphone/gateway-patch/`

Устаревшие зеркала в монорепо — источник правды **только этот каталог**.
