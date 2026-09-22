# gateway-web-softphone

Python module: **веб-софтфон (статика + same-origin `/api` + сессия)** внутри **того же процесса**, что и **Callspire PBX Gateway** (FastAPI). Заменяет отдельный сервис **softphone-bff** без дублирования логики: внутренние запросы идут в тот же ASGI-приложение через `httpx.ASGITransport`.

## Установка

Из каталога репозитория gateway (или через `pip install -e`):

```bash
pip install -e ./gateway-web-softphone
```

Зависимости: `fastapi`, `httpx`, `starlette` (см. `pyproject.toml`).

## Интеграция в `app.py` gateway

1. Соберите UI: `cd softphone-web && npm run build` (должны появиться `index.html` и `assets/`).
2. В **самый конец** `app.py` (после всех `include_router`):

```python
from gateway_web_softphone import install_web_softphone

install_web_softphone(app)
```

Опционально явный каталог со статикой:

```python
install_web_softphone(app, static_dir="/opt/mikopbx-cdr-proxy/softphone-web")
```

Расширенная настройка — напрямую `mount_web_softphone(app, WebSoftphoneConfig(...))`.

**Важно:** вызывайте **один раз**, **после** регистрации всех маршрутов gateway и по возможности **последним** среди middleware, чтобы сессия оборачивала приложение снаружи.

Если маршрут `GET /` уже занят: `export WEB_SOFTPHONE_SKIP_ROOT_REDIRECT=1`.

Отключить монтирование без правки кода: `export WEB_SOFTPHONE_ENABLED=0`.

## Переменные окружения

Совместимы с **softphone-bff** там, где это имеет смысл:

| Переменная | Назначение |
|------------|------------|
| `SESSION_SECRET` | Подпись cookie-сессии (обязательно в проде). |
| `SESSION_NAME` | Имя cookie (по умолчанию `callspire_softphone_sid`). |
| `SESSION_SECURE` | `true` / `false` — флаг Secure у cookie (за HTTPS выставьте `true`). |
| `SOFTPHONE_STATIC_DIR` | Каталог с `dist` SPA (по умолчанию `./softphone-web/dist` от cwd). |
| `PBX_GATEWAY_SERVICE_TOKEN` | Опционально, заголовок `X-Callspire-Service-Token` на внутренние вызовы (как у BFF). |
| `WEBRTC_SIP_WS_URL`, `WEBRTC_SIP_HOST` | Fallback WSS/SIP host в `/api/webrtc/config`, если не задано в админке gateway. |
| `WEBRTC_TURN_URLS`, `WEBRTC_TURN_USERNAME`, `WEBRTC_TURN_PASSWORD` | TURN/STUN для WebRTC: gateway отдаёт `iceServers` в `GET /api/v1/webrtc/config` (браузерный `/api/webrtc/config` проксирует как есть). |
| `TRUST_PROXY` | Оставлено в конфиге для документации; для uvicorn за reverse-proxy используйте `--proxy-headers` или `ProxyHeadersMiddleware`. |
| `WEB_SOFTPHONE_ENABLED` | `0` / `false` — не монтировать веб-софтфон. |
| `WEB_SOFTPHONE_SKIP_ROOT_REDIRECT` | `1` — не регистрировать редирект `GET /` → `/softphone/` (если `/` уже занят). |

Отдельный `PBX_GATEWAY_BASE_URL` **не нужен** — вызовы идут в тот же `app`.

## URL для пользователей

- UI: `https://<host>/softphone/`
- Редирект с `/` на `/softphone/`
- API браузера: `/api/...` (как в README softphone-bff)

Если у gateway уже занят маршрут `GET /`, задайте свой редирект или измените код монтирования (в `mount.py` зарегистрирован `@app.get("/")`).

## Отказ от Node BFF

После переноса на gateway можно не запускать **softphone-bff**; nginx должен проксировать **один** upstream на uvicorn gateway (порт из вашего unit-файла).

## Ограничения

- Сессия Starlette хранится в **подписанном cookie** (как и типичный BFF с MemoryStore — но там данные на сервере). При очень длинном JWT следите за размером cookie (~4KB лимит браузера). При необходимости позже можно заменить на серверное хранилище сессий.
- Внутренние вызовы не пересылают cookie браузера в подзапрос ASGI (и не должны) — авторизация к API gateway идёт через `Authorization: Bearer` из сессии.
