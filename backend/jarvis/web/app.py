"""Веб-панель Jarvis: FastAPI-приложение (ЧАСТЬ 6).

    create_app(assistant, policy, scheduler=None, log=None) -> FastAPI

Слушает ТОЛЬКО localhost (config.WEB_HOST/WEB_PORT). Опциональный токен
(JARVIS_WEB_TOKEN) защищает от локальных процессов; опасные действия
всё равно проходят через Confirmator политики.

Запуск: python -m jarvis.main web   (см. main.py)
"""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from jarvis import logger

from jarvis.web.routes import chat as chat_routes
from jarvis.web.routes import logs as logs_routes
from jarvis.web.routes import reminders as reminders_routes
from jarvis.web.routes import settings as settings_routes

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(assistant, policy, scheduler=None, log=None,
               reminder_store=None):
    """Собирает FastAPI-приложение с роутами и статикой."""
    app = FastAPI(title='Jarvis Web', version='1.0',
                  docs_url=None, redoc_url=None)

    app.state.assistant = assistant
    app.state.policy = policy
    app.state.scheduler = scheduler
    app.state.log = log or logger.get_logger()

    # ReminderStore живёт в планировщике; для веба создаём свой —
    # SQLite (WAL) позволяет читать из нескольких процессов
    if reminder_store is None:
        from jarvis.proactive.reminders import ReminderStore
        reminder_store = ReminderStore()
    app.state.reminder_store = reminder_store

    app.include_router(chat_routes.router)
    app.include_router(reminders_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(logs_routes.router)

    app.mount('/static', StaticFiles(
        directory=os.path.join(_PACKAGE_DIR, 'static')), name='static')

    @app.get('/')
    def index():
        """Одностраничная панель (HTML+JS, без фреймворков)."""
        return FileResponse(os.path.join(_PACKAGE_DIR, 'templates',
                                         'index.html'))

    return app