"""Защита веб-панели (ЧАСТЬ 6).

Панель слушает ТОЛЬКО localhost. Если задан JARVIS_WEB_TOKEN — все
запросы должны нести Authorization: Bearer <token> (HTTP) или
?token=<token> (WebSocket). Токен сравнивается в постоянном времени,
чтобы не светить время сравнения через замер.
"""

import hmac

from fastapi import Header, HTTPException, WebSocket, WebSocketDisconnect

from jarvis import config


def _matches(provided, expected):
    return hmac.compare_digest((provided or '').encode('utf-8'),
                               expected.encode('utf-8'))


def require_token(authorization: str | None = Header(default=None)):
    """FastAPI-зависимость: проверяет Authorization: Bearer <token>."""
    if not config.WEB_TOKEN:
        return
    token = ''
    if authorization and authorization.lower().startswith('bearer '):
        token = authorization[7:].strip()
    if not _matches(token, config.WEB_TOKEN):
        raise HTTPException(status_code=401, detail='Требуется токен.')


def check_ws_token(websocket: WebSocket):
    """Проверка токена WebSocket (query-параметр ?token=)."""
    if not config.WEB_TOKEN:
        return
    token = websocket.query_params.get('token', '')
    if not _matches(token, config.WEB_TOKEN):
        raise WebSocketDisconnect(code=4401, reason='Требуется токен.')