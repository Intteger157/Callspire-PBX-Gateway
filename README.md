# Callspire PBX Gateway + веб-софтфон

Здесь лежит **всё, что относится к встраиванию веб-UI в gateway**. Основной код gateway (`app.py`, `config.yaml`, работа с MikoPBX) у вас может жить в отдельном клоне репозитория — эту папку можно **скопировать в корень того репозитория** как есть (или держать submodule).

## Что лежит в каталоге

```
callspire-pbx-gateway/
├── README.md                    ← вы здесь: карта и шаги
├── requirements-web-softphone.txt
├── INTEGRATION_APP_EXAMPLE.py   ← фрагмент для вставки в app.py
└── gateway-web-softphone/       ← Python-пакет (подключается в FastAPI)
    ├── pyproject.toml
    ├── README.md                ← детали API / env
    └── gateway_web_softphone/
        ├── __init__.py
        ├── install.py           ← install_web_softphone(app) — одна строка в app.py
        └── mount.py
```

**Фронт (Vue/React и т.д.)** в этом репозитории не лежит: он в проекте **softphone-web** (репо `callspire-web-softphone` или соседняя папка). Собранный UI должен оказаться в каталоге **`dist`** (часто `softphone-web/dist`).

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

- `mdkb.amocrm.ru` (RU accounts)
- `mdkb.kommo.com` (global Kommo)
- `mdkb` (short name → defaults to `.amocrm.ru`)

Filled automatically after **Authorize with Kommo**.

### Per-user exclusions

**PBX Users** → column **Gateway Kommo** → uncheck **Use shared** to exclude an extension from the company Kommo session (they can still use local Kommo in the desktop app).

### Deploy files

After changes to Kommo auth, copy `kommo_oauth.py`, `kommo_service.py`, `app_kommo.py`, `permissions_db.py`, and `templates/admin.html`, then restart the gateway service.

---

## Если gateway в другом Git-репозитории

Скопируйте **всю** папку `callspire-pbx-gateway` (или только `gateway-web-softphone` + `requirements-web-softphone.txt` + пример интеграции) в корень того репозитория и закоммитьте там.
