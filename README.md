# Callspire PBX Gateway

FastAPI-сервис между MikoPBX, веб/десктоп-софтфоном, мобильным IGCaller и Kommo (AmoCRM): CDR, originate, OAuth Kommo, фоновые workers для записей и звонков, админка, прокси обновлений APK.

**Этот репозиторий — единственный источник правок gateway.**  
Не правьте и не деплойте копии из `deploy/pbx-gateway-kommo-bundle/` или `deploy/web-softphone/gateway-patch/` в монорепозитории Softphone — там только зеркала/черновики.

## Продакшен

На сервере обычно:

- `/opt/mikopbx-cdr-proxy/` — рабочий каталог (systemd `mikopbx-cdr`)
- либо `/opt/callspire-pbx-gateway/` — то же приложение, другое имя пути

Перед обновлением сделайте бэкап каталога (например `tar czf bkp-YYYYMMDD-HHMMSS.tar.gz …`). Файлы `bkp-*.tar.gz` в git не коммитятся.

## Конфигурация и секреты

| Файл | В git |
|------|--------|
| `config.example.yaml` | да — шаблон |
| `config.yaml` | **нет** — пароли, JWT, Miko API, пути к CDR/записям |
| `.env.example` | да |
| `.env` | **нет** — Web softphone session, TURN и т.п. |
| `*.sqlite`, `permissions.db` | **нет** — jobs Kommo, OAuth, mobile releases token |

Скопируйте `config.example.yaml` → `config.yaml` и заполните на сервере. GitHub token для mobile releases хранится в SQLite через админку, не в репозитории.

## Деплой

Обновление **вручную** (WinSCP, rsync, scp): только изменённые файлы, не «все `.py` разом».

- Копируйте из **этого** репозитория в корень сервиса на сервере.
- Не перезаписывайте `app.py` целиком без проверки — на проде могут быть локальные отличия; для Kommo часто достаточно `app_kommo.py` и модулей `kommo_*`.
- После копирования: `source venv/bin/activate`, при необходимости `pip install -r requirements.txt`, перезапуск systemd.

Подробный чеклист, откат и типичные ошибки: **[DEPLOY_CHECKLIST.ru.md](DEPLOY_CHECKLIST.ru.md)**.

## Основные модули

| Область | Файлы |
|---------|--------|
| HTTP / JWT / CDR API | `app.py`, `cdr_client.py`, `auth.py`, `config.py` |
| Kommo process-call | `app_kommo.py`, `kommo_call_worker.py`, `kommo_jobs_db.py`, `kommo_crm.py`, `kommo_recording.py` |
| Входящие из CDR без softphone | `kommo_cdr_entity_worker.py` |
| Web softphone mount | `gateway-web-softphone/` |
| Mobile APK updates (GitHub) | `app_mobile_releases.py`, `mobile_releases_*.py` |
| Админ UI | `templates/admin*.html` |

Стабильный контракт для клиентов (desktop / web / mobile): `POST/GET /api/kommo/process-call`, `GET /api/kommo/status`, `GET /api/kommo/session` и связанные пути — без ломающих изменений URL и обязательных полей.

## Локально

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # и отредактировать
python app.py
```

Тесты dedup Kommo jobs (опционально): `python -m unittest test_kommo_job_dedup`.

## Связанные репозитории

- Desktop / Core — монорепозиторий Softphone (`Callspire.Core`, gateway mode).
- Mobile — [IntermarkCaller](https://github.com/IntermarkGlobal/IntermarkCaller) (IGCaller).
