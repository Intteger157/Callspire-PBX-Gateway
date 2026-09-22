# Деплой на production (`/opt/mikopbx-cdr-proxy/`)

Цель: веб-софтфон + новый Kommo upload **без потери** назначенных CallerID и маппинга extension → Kommo user.

## Что хранит ваши настройки (НЕ ТРОГАТЬ)

Все назначения с экрана **PBX Users** (CallerIDs, Gateway Kommo, Kommo user) лежат в **`permissions.db`**:

| Таблица | Содержимое |
|---------|------------|
| `callerid_permissions` | extension → номера (UK-Investments, Beeline-RU, …) |
| `callerid_names` | подписи номеров |
| `kommo_extension_users` | extension → kommo_user_id / name |
| `kommo_integration` + `kommo_oauth_tokens` | OAuth Kommo |
| `app_users` | email-логины веб-софтфона |
| `ami_config` | AMI для originate |

`permissions_db.py` при старте только **добавляет** таблицы/колонки (`CREATE IF NOT EXISTS`, `ALTER`). Данные не удаляет.

**Никогда не копируйте** `permissions.db` с dev на prod и не затирайте prod-файл.

---

## Шаг 0 — бэкап на prod (обязательно)

```bash
cd /opt/mikopbx-cdr-proxy
chmod +x scripts/backup-gateway.sh
sudo ./scripts/backup-gateway.sh --tar
```

Скрипт кладёт снимок в `backups/bkp-YYYYMMDD-HHMMSS/` (код, `config.yaml`, SQLite через
`sqlite3 .backup`, templates, UI). Архив — рядом, `backups/bkp-....tar.gz`. Старые снимки
удаляются автоматически (последние 10; `--keep N`).

Быстрый бэкап только БД (если меняете одну таблицу):

```bash
sudo cp permissions.db "permissions.db.bak-$(date +%Y%m%d-%H%M)"
sudo cp config.yaml "config.yaml.bak-$(date +%Y%m%d-%H%M)"
```

Проверка «сколько назначений было»:

```bash
sqlite3 permissions.db "SELECT COUNT(*) FROM callerid_permissions;"
sqlite3 permissions.db "SELECT COUNT(*) FROM kommo_extension_users;"
sqlite3 permissions.db "SELECT extension, COUNT(*) FROM callerid_permissions GROUP BY extension LIMIT 5;"
```

Запишите числа — после деплоя должны совпасть.

---

## Шаг 1 — залить код (с вашей машины)

Источник: **`callspire-pbx-gateway/`** (не `deploy/`).

```bash
cd callspire-pbx-gateway
chmod +x scripts/deploy-to-prod.sh

# SPA: сначала npm run build в callspire-web-softphone/softphone-web
export SOFTPHONE_DIST="../callspire-web-softphone/softphone-web/dist"
./scripts/deploy-to-prod.sh miko@vultr /opt/mikopbx-cdr-proxy
```

Или вручную через `rsync` / `scp` — **исключая** `permissions.db`, `config.yaml`, `venv/`.

### Новые файлы (добавить)

- `gateway-web-softphone/` (весь каталог)
- `kommo_crm.py`, `kommo_recording.py`, `kommo_call_worker.py`, `kommo_jobs_db.py`, `kommo_store.py`

### Заменить

- `app.py`, `app_kommo.py`, `permissions_db.py`
- `gateway-web-softphone/gateway_web_softphone/mount.py`
- `templates/admin_kommo.html` (и при необходимости `admin.html`)

### Не удалять на prod (пока)

- `kommo_oauth.py`, `kommo_service.py` — старый OAuth, не мешают
- `mobile_v1.py` — **если мобильное приложение ещё использует**; новый `app.py` его не подключает. Перед деплоем: `grep mobile_v1 app.py` на prod. Если есть — после деплоя вернуть `include_router` из бэкапа `app.py.bak-*`.

### Старые Kommo (можно переименовать после успешного деплоя)

- `kommo_crm_client.py` → `.bak`
- `kommo_recording_push.py` → `.bak`

---

## Шаг 2 — venv и пакет web-softphone

```bash
cd /opt/mikopbx-cdr-proxy
source venv/bin/activate
pip install -r requirements.txt
pip install -e ./gateway-web-softphone
pip install httpx itsdangerous   # если вдруг не подтянулись

python -c "import gateway_web_softphone; import kommo_crm, app_kommo; print('imports OK')"
python -c "import gateway_web_softphone.mount as m; print(m.__file__)"
grep '_callspire_internal_asgi' gateway-web-softphone/gateway_web_softphone/mount.py
```

---

## Шаг 3 — переменные окружения (systemd)

Создайте `/etc/systemd/system/mikopbx-cdr.service.d/softphone.conf`:

```ini
[Service]
Environment=SESSION_SECRET=СГЕНЕРИРУЙТЕ_ДЛИННУЮ_СЛУЧАЙНУЮ_СТРОКУ
Environment=SOFTPHONE_STATIC_DIR=/opt/mikopbx-cdr-proxy/softphone-web/dist
Environment=SESSION_SECURE=true
Environment=WEB_SOFTPHONE_ENABLED=1
# Если GET / уже занят nginx — раскомментируйте:
# Environment=WEB_SOFTPHONE_SKIP_ROOT_REDIRECT=1
```

```bash
sudo systemctl daemon-reload
```

`config.yaml` **не перезаписывать** — там prod paths, jwt_secret, REST, порт.

---

## Шаг 4 — nginx (убрать старый BFF)

Сейчас: `/opt/callspire-softphone/softphone-bff` + старый `callspire-web`.

Нужно: **один upstream** на uvicorn gateway (порт из `config.yaml` в prod).

Пример фрагмента:

```nginx
location /softphone/ {
    proxy_pass http://127.0.0.1:ПОРТ_ИЗ_config.yaml;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

API браузера: **`/softphone/api/*`** (не `/api/*` — там desktop JWT).

После проверки UI:

```bash
sudo systemctl stop softphone-bff   # если был отдельный unit
# или просто не проксировать на BFF
sudo nginx -t && sudo systemctl reload nginx
```

---

## Шаг 5 — рестарт и проверка

```bash
sudo systemctl restart mikopbx-cdr
sudo journalctl -u mikopbx-cdr -n 40 --no-pager
```

Порт gateway:

```bash
grep '^port:' config.yaml
```

Проверки:

```bash
PORT=$(grep '^port:' config.yaml | awk '{print $2}')
curl -s "http://127.0.0.1:${PORT}/softphone/api/health"
curl -s "http://127.0.0.1:${PORT}/openapi.json" | grep -o '"/softphone/api[^"]*"' | head

# caller IDs / Kommo mappings сохранились?
sqlite3 permissions.db "SELECT COUNT(*) FROM callerid_permissions;"
sqlite3 permissions.db "SELECT COUNT(*) FROM kommo_extension_users;"
```

Логин (реальный email из **Web Softphone → Accounts**):

```bash
curl -s -c /tmp/sid.jar -X POST "http://127.0.0.1:${PORT}/softphone/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"USER@example.com","password":"..."}'
curl -s -b /tmp/sid.jar "http://127.0.0.1:${PORT}/softphone/api/my-callerids"
```

В UI: Admin → PBX Users — те же CallerIDs и Kommo user, что до деплоя.

---

## Откат

```bash
cd /opt/mikopbx-cdr-proxy
sudo cp permissions.db.bak-YYYYMMDD permissions.db   # только если БД повредили
sudo cp config.yaml.bak-YYYYMMDD config.yaml
# восстановить app.py из tar-бэкапа
sudo systemctl restart mikopbx-cdr
```

---

## Чеклист

- [ ] Бэкап `permissions.db` + `config.yaml`
- [ ] Записаны COUNT до деплоя
- [ ] Код из `callspire-pbx-gateway/`, не `deploy/`
- [ ] `pip install -e ./gateway-web-softphone`
- [ ] `SESSION_SECRET` + `SOFTPHONE_STATIC_DIR` в systemd
- [ ] nginx → gateway, не BFF
- [ ] COUNT после деплоя совпадает
- [ ] Admin PBX Users без изменений
- [ ] Web softphone: login, CDR, WebRTC register
