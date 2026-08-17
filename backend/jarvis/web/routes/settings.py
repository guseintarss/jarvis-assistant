"""Роуты настроек и статуса: policy.yaml через UI (ЧАСТЬ 6).

Правила безопасности:
    • менять можно ТОЛЬКО включение/выключение инструмента и его риск;
    • значение риска валидируется (low/medium/high);
    • изменения применяются к ЖИВОМУ объекту политики сразу (set_tool)
      и сохраняются в policy.yaml (если файл доступен на запись);
    • статус не раскрывает секреты: только имена провайдеров и счётчики.
"""

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jarvis import config
from jarvis import intents
from jarvis.web.security import require_token

router = APIRouter()

_RISKS = (intents.RISK_LOW, intents.RISK_MEDIUM, intents.RISK_HIGH)


class ToolUpdate(BaseModel):
    name: str
    enabled: bool | None = None
    risk: str | None = None


def _policy(request: Request):
    return request.app.state.policy


def _tools_view(policy):
    """Текущие настройки инструментов: из policy.yaml + дефолты интентов."""
    out = {}
    for name, intent in intents.INTENTS.items():
        spec = policy.tool_spec(name) or {}
        out[name] = {
            'enabled': policy.tool_enabled(name),
            'risk': policy.tool_risk(name),
            'description': intent.description,
            'default_risk': intent.risk,
        }
    return out


@router.get('/api/settings')
def get_settings(request: Request, _token_ok=Depends(require_token)):
    """Настройки инструментов + путь к policy.yaml."""
    return {
        'policy_path': request.app.state.policy.source,
        'tools': _tools_view(request.app.state.policy),
    }


@router.put('/api/settings/tool')
def update_tool(body: ToolUpdate, request: Request,
                _token_ok=Depends(require_token)):
    """Обновляет инструмент: включение/выключение, риск. Живое + файл."""
    if body.name not in intents.INTENTS:
        raise HTTPException(status_code=404,
                            detail=f'Неизвестный инструмент: {body.name}')
    if body.enabled is None and body.risk is None:
        raise HTTPException(status_code=422,
                            detail='Укажите enabled или risk.')
    policy = request.app.state.policy
    if not policy.set_tool(body.name, enabled=body.enabled, risk=body.risk):
        raise HTTPException(status_code=422, detail='Некорректные значения.')

    saved = _persist_tool(request.app.state.policy.source,
                          body.name, body.enabled, body.risk)
    return {'name': body.name, 'saved_to_file': saved,
            'enabled': policy.tool_enabled(body.name),
            'risk': policy.tool_risk(body.name)}


def _persist_tool(policy_path, name, enabled, risk):
    """Сохраняет изменение в policy.yaml. True — записано; False — файл
    недоступен на запись (изменение осталось только в памяти)."""
    import yaml
    try:
        with open(policy_path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return False
    target = raw if 'policy' not in raw else raw['policy']
    tools = target.setdefault('tools', {})
    spec = dict(tools.get(name) or {})
    if enabled is not None:
        spec['enabled'] = enabled
    if risk is not None:
        spec['risk'] = risk
    tools[name] = spec
    try:
        with open(policy_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(raw, f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
        return True
    except OSError:
        return False


@router.get('/api/status')
def status(request: Request, _token_ok=Depends(require_token)):
    """Статус системы: цепочка облака, память, индекс, планировщик."""
    app_state = request.app.state
    status = {
        'policy_source': app_state.policy.source,
        'memory_enabled': bool(app_state.assistant.memory
                               and getattr(app_state.assistant.memory,
                                           'enabled', False)),
        'index': _index_stats(),
    }
    cloud = app_state.assistant.cloud
    if cloud is not None:
        router = getattr(cloud, 'router', None)
        status['cloud'] = {
            'chain': router.chain() if router else [],
            'stats': dict(router.stats) if router else {},
        }
    else:
        status['cloud'] = None
    scheduler = app_state.scheduler
    if scheduler is not None:
        status['scheduler'] = {
            'running': scheduler.is_running(),
            'pending_reminders': len(scheduler.pending()),
            'stats': scheduler.stats,
        }
    else:
        status['scheduler'] = None
    return status


def _index_stats():
    """Счётчик файлов в индексе (0 — индекс не собран)."""
    try:
        conn = sqlite3.connect(f'file:{config.INDEX_DB_PATH}?mode=ro',
                               uri=True, timeout=2)
        try:
            return {'files': conn.execute(
                'SELECT count(*) FROM files_fts').fetchone()[0]}
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {'files': 0}