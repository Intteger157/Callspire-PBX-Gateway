# Перенесено в callspire-pbx-gateway

Канонический каталог gateway (Kommo, CDR, workers):

**[`callspire-pbx-gateway/`](../../callspire-pbx-gateway/)**

Деплой — вручную (WinSCP и т.п.) только нужные файлы в `/opt/mikopbx-cdr-proxy/`.
Не копируйте все `*.py` разом и не перезаписывайте `app.py` без `app_kommo.py`.

Подробности: `DEPLOY_CHECKLIST.ru.md`.
