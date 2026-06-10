# Скопируйте в конец app.py (после всех роутеров):
#
#   from gateway_web_softphone import install_web_softphone
#   install_web_softphone(app)
#
# Опционально с явным путём к каталогу со сборкой UI (index.html):
#
#   install_web_softphone(app, static_dir="/opt/mikopbx-cdr-proxy/softphone-web")
#
# Если у приложения уже занят маршрут GET /, задайте:
#   export WEB_SOFTPHONE_SKIP_ROOT_REDIRECT=1
#
# Полностью отключить веб-софтфон без правки кода:
#   export WEB_SOFTPHONE_ENABLED=0
