"""Однострочное подключение веб-софтфона к FastAPI-приложению gateway."""

from __future__ import annotations

import os
from pathlib import Path

from gateway_web_softphone.mount import WebSoftphoneConfig, mount_web_softphone


def install_web_softphone(app, *, static_dir: Path | str | None = None) -> None:
    """
    Вызовите **в самом конце** ``app.py``, после всех ``include_router``.

    Пример::

        from gateway_web_softphone import install_web_softphone
        install_web_softphone(app)

    Отключить монтирование без правки кода: переменная окружения ``WEB_SOFTPHONE_ENABLED=0``.

    Явный каталог со сборкой SPA (если не хотите полагаться только на ``SOFTPHONE_STATIC_DIR``)::

        install_web_softphone(app, static_dir="/opt/mikopbx-cdr-proxy/softphone-web")
    """
    raw = os.environ.get("WEB_SOFTPHONE_ENABLED", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return

    sd: Path | None = None
    if static_dir is not None:
        sd = Path(static_dir).expanduser().resolve()

    cfg = WebSoftphoneConfig.from_env(static_dir=sd)
    mount_web_softphone(app, cfg)
