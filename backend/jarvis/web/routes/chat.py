"""Роуты чата: REST, WebSocket и история (ЧАСТЬ 6).

WebSocket /ws/chat — real-time: клиент шлёт текст, сервер отвечает
результатом обработки (response/intent/route). Обработка синхронная
(assistant.process), но на обычном тексте это миллисекунды; для
тяжёлых облачных запросов — стандартный таймаут пайплайна.
"""

from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel

from jarvis.web.security import require_token, check_ws_token

router = APIRouter()


class ChatRequest(BaseModel):
    text: str


class _Routes:
    """Держатель зависимостей — упрощает моки в тестах."""


def _assistant(request: Request):
    return request.app.state.assistant


@router.post('/api/chat')
def chat(body: ChatRequest, request: Request,
         _token_ok=Depends(require_token)):
    """Текстовый запрос к Jarvis. (response, intent, confidence, route, ok)."""
    result = request.app.state.assistant.process(body.text[:2000])
    return result


@router.websocket('/ws/chat')
async def ws_chat(websocket: WebSocket):
    """Real-time чат: текст -> {response, intent, confidence, route, ok}."""
    check_ws_token(websocket)
    await websocket.accept()
    assistant = websocket.app.state.assistant
    try:
        while True:
            text = (await websocket.receive_text()).strip()
            if not text:
                continue
            try:
                result = assistant.process(text[:2000])
            except Exception as exc:  # noqa: BLE001 — не ронять сокет
                result = {'response': f'Ошибка обработки: {exc}',
                          'intent': 'error', 'confidence': 0.0,
                          'route': 'error', 'ok': False}
            await websocket.send_json(result)
    except Exception:  # noqa: BLE001 — клиент отключился (в т.ч. по токену)
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get('/api/history')
def history(request: Request, _token_ok=Depends(require_token)):
    """История диалога из памяти (короткий контекст, максимум 20 реплик)."""
    memory = request.app.state.assistant.memory
    if memory is None or not getattr(memory, 'enabled', False):
        return {'turns': [], 'facts': {}, 'last_actions': []}
    return memory.context()