"""Роуты логов: просмотр JSONL через веб-панель (ЧАСТЬ 6).

Секреты маскируются при записи (logger.redact) — на чтение отдаются
уже безопасные записи. Отдаём хвост сегодняшнего файла, максимум
config.WEB_LOG_LINES_MAX строк.
"""

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from jarvis import config
from jarvis.web.security import require_token

router = APIRouter()


def _log_path(request: Request):
    log = request.app.state.log
    return os.path.join(log.log_dir, f'jarvis-{_today()}.jsonl')


def _today():
    import datetime
    return datetime.date.today().isoformat()


@router.get('/api/logs')
def logs(request: Request, lines: int = Query(200, ge=1, le=500),
         _token_ok=Depends(require_token)):
    """Последние строки сегодняшнего лога как список событий."""
    path = _log_path(request)
    try:
        with open(path, encoding='utf-8') as f:
            raw_lines = f.readlines()[-lines:]
    except OSError:
        raise HTTPException(status_code=404, detail='Лог за сегодня не найден.')
    events = []
    for line in raw_lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # битая строка пропускается
    return {'file': path, 'events': events}