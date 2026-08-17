"""Клиент облачной LLM: возвращает ТОЛЬКО JSON-план действий.

Принцип безопасности: облако никогда не получает инструменты исполнения —
ему передаётся:
    • user_request (текст пользователя);
    • environment (ОС, сессия, рабочий стол, доступные утилиты);
    • available_tools (имена + описания + типы параметров);
    • policy (включённые инструменты, уровни риска, подтверждения).

Ответ облака — JSON {explanation, steps: [{tool, params, reason, confirm}]},
который проходит строгую валидацию (plan.validate_plan) и только потом
исполняется локальным Executor. Сырые команды модель вернуть не может:
поля command/shell/script/exec запрещены валидатором, а путей вне
allowed_roots не существует в её «вселенной».

Приватные данные не отправляются: ни переменные окружения, ни содержимое
~/.ssh, ~/.config и т.п. — в запрос попадает только снимок окружения,
собранный функциями ниже.
"""

import json
import os
import platform
import re
import time

import requests

from jarvis import config
from jarvis import logger
from jarvis import plan as plan_mod

# Паттерн «вытащить JSON из ответа» (модели иногда оборачивают в ```json)
_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def environment_snapshot():
    """Безопасный снимок окружения для облака. Никаких секретов.

    Сознательно НЕ передаём: env-переменные, имена пользователя в путях
    (заменяются на ~), сетевые адреса.
    """
    def have(*names):
        return [n for n in names if shutil.which(n)]

    import shutil
    session = os.environ.get('XDG_SESSION_TYPE', 'unknown')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')
    try:
        with open('/etc/os-release', encoding='utf-8') as f:
            distro = next((l.split('=', 1)[1].strip().strip('"')
                           for l in f if l.startswith('NAME=')), 'unknown')
    except OSError:
        distro = 'unknown'
    return {
        'os': f'{distro} (kernel {platform.release()})',
        'session': session,                     # x11 / wayland
        'desktop': desktop,
        'python': platform.python_version(),
        'tools': {
            'volume': have('wpctl', 'pactl'),
            'brightness': have('brightnessctl'),
            'media': have('playerctl'),
            'notify': have('notify-send'),
            'open': have('xdg-open'),
            'search': have('fd', 'rg'),
            'trash': have('gio'),
            'screenshot': have('gnome-screenshot', 'grim', 'import'),
        },
    }


def _system_prompt(policy, registry):
    """Системный промпт: жёсткая схема JSON-плана и правила."""
    tools = '\n'.join('  - ' + t for t in registry.describe_all()) \
        or '  (нет инструментов)'
    risks = {
        'low': 'выполняется без подтверждения',
        'medium': 'проверка путей, выполняется без подтверждения',
        'high': 'требует подтверждения пользователя (в плане отметьте confirm=true)',
    }
    return f"""Ты — планировщик локального десктоп-ассистента Jarvis на Linux. \
Пользователь написал запрос, а ты должен решить, какие инструменты вызвать.

ЖЁСТКИЕ ПРАВИЛА:
1. Твой ответ — ТОЛЬКО валидный JSON, без markdown-обёрток и пояснений вне JSON.
2. Формат строго такой:
   {{"explanation": "краткое объяснение плана (по-русски, 1-2 предложения)",
     "steps": [{{"tool": "<имя инструмента>", "params": {{...}},
                "reason": "зачем этот шаг", "confirm": <bool>}}]}}
3. Если действий не нужно — верни {{"explanation": "ответ на запрос", "steps": []}}.
   Тогда explanation станет ответом пользователю.
4. Используй ТОЛЬКО инструменты из списка ниже. Не выдумывай имена.
5. НИКОГДА не возвращай поля command/shell/script/exec/url-схемы кроме
   http/https — валидатор отклонит план.
6. Пути — только в пределах домашнего каталога пользователя (~), без
   ~/.ssh, ~/.config и прочих приватных каталогов.
7. Если для шага нужен код — используй инструмент run_code с полем
   params.content (Python). Код будет выполнен в песочнице без сети.
8. Риски инструментов: low/medium — выполняй сразу; high — ставь confirm=true.
9. explanation всегда на русском языке.

Доступные инструменты:
{tools}

Правила рисков:
{risks}"""


class CloudLLM:
    """Облачная LLM, возвращающая валидированный JSON-план."""

    def __init__(self, policy, registry, guard, log=None):
        self.policy = policy
        self.registry = registry
        self.guard = guard
        self.log = log or logger.get_logger()
        self._session = requests.Session()

    # --------------------------- запрос ------------------------------------

    def request_plan(self, user_request):
        """user_request -> (ok, plan|None, message).

        plan уже прошёл validate_plan. При недоступности облака ok=False
        и message объясняет проблему пользователю.
        """
        if not self.policy.cloud.get('enabled', True) or not config.CLOUD_ENABLED:
            return False, None, 'Облачная модель отключена политикой.'

        payload = self._build_payload(user_request)
        last_error = 'облако не ответило'
        for attempt in range(config.CLOUD_RETRIES + 1):
            try:
                resp = self._session.post(
                    f'{config.CLOUD_BASE_URL}/chat/completions',
                    headers=self._headers(),
                    json=payload,
                    timeout=config.CLOUD_TIMEOUT_SEC)
                if resp.status_code == 429:
                    last_error = 'облако перегружено (429), попробую ещё раз'
                    time.sleep(config.CLOUD_RETRY_DELAY_SEC)
                    continue
                resp.raise_for_status()
                content = self._extract_content(resp.json())
                raw = self._parse_json(content)
                if raw is None:
                    last_error = ('модель вернула не-JSON ответ; '
                                  'план отклонён')
                    continue
                ok, error, plan = plan_mod.validate_plan(
                    raw, self.policy, self.registry, self.guard)
                if not ok:
                    last_error = f'план отклонён валидатором: {error}'
                    self.log.event('cloud_plan_rejected', error=error)
                    continue
                self.log.event('cloud_plan', steps=len(plan['steps']))
                return True, plan, ''
            except requests.RequestException as exc:
                last_error = f'ошибка сети: {exc}'
            except (ValueError, KeyError, TypeError) as exc:
                last_error = f'неожиданный ответ облака: {exc}'

        self.log.event('cloud_failed', error=last_error)
        return False, None, last_error

    # --------------------------- сборка ------------------------------------

    def _headers(self):
        headers = {'Content-Type': 'application/json'}
        if config.CLOUD_API_KEY:
            headers['Authorization'] = f'Bearer {config.CLOUD_API_KEY}'
        return headers

    def _build_payload(self, user_request):
        tools = [t.describe() for t in
                 (self.registry.get(n) for n in self.registry.enabled_names())
                 if t is not None]
        user_msg = {
            'user_request': user_request,
            'environment': environment_snapshot(),
            'available_tools': tools,
            'policy': {
                'enabled_tools': self.registry.enabled_names(),
                'confirmation': self.policy.confirmation,
                'allowed_roots': ['~' + p.replace(os.path.expanduser('~'), '')
                                  for p in self.policy.allowed_roots],
                'denied_paths': ['~' + p.replace(os.path.expanduser('~'), '')
                                 for p in self.policy.denied_paths],
                'sandbox': {k: self.policy.sandbox.get(k)
                            for k in ('enabled', 'time_limit_sec',
                                      'memory_limit_mb', 'network')},
            },
        }
        return {
            'model': config.CLOUD_MODEL,
            'messages': [
                {'role': 'system',
                 'content': _system_prompt(self.policy, self.registry)},
                {'role': 'user',
                 'content': json.dumps(user_msg, ensure_ascii=False)},
            ],
            'temperature': 0.2,
            'max_tokens': config.CLOUD_MAX_TOKENS,
        }

    # --------------------------- разбор ------------------------------------

    @staticmethod
    def _extract_content(data):
        """content модели; reasoning_content игнорируется (это «мысли»)."""
        choice = data['choices'][0]
        message = choice.get('message', {})
        return message.get('content') or ''

    @staticmethod
    def _parse_json(content):
        content = (content or '').strip()
        if not content:
            return None
        # снять markdown-обёртку ```json ... ```
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content).strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = _JSON_RE.search(content)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
            return None