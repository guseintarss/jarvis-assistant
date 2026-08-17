"""Роуты напоминаний: просмотр и управление через веб-панель (ЧАСТЬ 6).

Используется тот же ReminderStore, что у инструментов и планировщика —
единый источник правды. Напоминания, добавленные из веба, срабатывают
так же, как голосовые/текстовые.
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jarvis.proactive.reminders import ReminderStore
from jarvis.web.security import require_token

router = APIRouter()


class ReminderCreate(BaseModel):
    when: str  # ISO 'YYYY-MM-DDTHH:MM[:SS]'
    text: str


def _store(request: Request):
    return request.app.state.reminder_store


def _valid_when(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get('/api/reminders')
def list_reminders(request: Request, _token_ok=Depends(require_token)):
    """Активные напоминания, ближайшие первыми."""
    rows = _store(request).upcoming()
    return {'reminders': [{'id': rid, 'when': when, 'text': text}
                          for rid, when, text in rows]}


@router.post('/api/reminders')
def add_reminder(body: ReminderCreate, request: Request,
                 _token_ok=Depends(require_token)):
    """Создаёт напоминание. when — ISO; text — что напомнить."""
    when = _valid_when(body.when)
    if when is None:
        raise HTTPException(status_code=422,
                            detail='when должен быть ISO-временем.')
    text = (body.text or '').strip()
    if not text:
        raise HTTPException(status_code=422, detail='text не может быть пустым.')
    rid = _store(request).add(body.when, text)
    return {'id': rid, 'when': body.when, 'text': text}


@router.delete('/api/reminders')
def clear_reminders(request: Request, _token_ok=Depends(require_token)):
    """Отменяет все активные напоминания."""
    rows = _store(request).upcoming()
    _store(request).clear()
    return {'cancelled': len(rows)}


@router.delete('/api/reminders/{reminder_id}')
def delete_reminder(reminder_id: int, request: Request,
                    _token_ok=Depends(require_token)):
    """Отменяет напоминание по id."""
    store = _store(request)
    if not any(r[0] == reminder_id for r in store.upcoming()):
        raise HTTPException(status_code=404, detail='Напоминание не найдено.')
    store.delete(reminder_id)
    return {'deleted': reminder_id}