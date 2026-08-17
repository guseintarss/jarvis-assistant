"""Валидация JSON-планов, которые возвращает облачная LLM.

Облачная модель НИКОГДА не исполняет команды: она возвращает структуру
    {
      "explanation": "краткое объяснение плана",
      "steps": [
        {"tool": "open_url", "params": {"url": "https://..."},
         "reason": "почему этот шаг", "confirm": false}
      ]
    }

Валидатор проверяет:
    • структуру (это dict, есть steps: list, размер в пределах лимита);
    • инструменты существуют и включены политикой;
    • запрещённые поля (command/shell/script/exec/...) отсутствуют —
      защита от попыток модели «выполнить сырую команду»;
    • пути внутри шагов проходят PathGuard (allow/deny);
    • URL — только http/https;
    • код — только через специальный шаг run_code (sandbox), с лимитом
      размера; обычные шаги кода запрещены.
"""

from jarvis import intents
from jarvis.security import check_params


FORBIDDEN_KEYS = ('command', 'shell', 'script', 'exec', 'subprocess',
                  'os_system', 'cmd', 'bash')


def _check_forbidden(obj, path='plan'):
    """Ищет запрещённые ключи на любом уровне структуры."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower().strip() in FORBIDDEN_KEYS:
                return f'{path}.{key}: запрещённое поле «{key}»'
            found = _check_forbidden(value, f'{path}.{key}')
            if found:
                return found
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found = _check_forbidden(item, f'{path}[{i}]')
            if found:
                return found
    return None


def validate_plan(raw, policy, registry, guard):
    """Проверяет план. Возвращает (ok, error|None, normalized_plan)."""
    if not isinstance(raw, dict):
        return False, 'План должен быть JSON-объектом.', None

    forbidden = _check_forbidden(raw)
    if forbidden:
        return False, forbidden, None

    steps_raw = raw.get('steps')
    if not isinstance(steps_raw, list) or not steps_raw:
        # Пустой план = «действий не требуется» — валидно (просто ответ)
        return True, None, {'explanation': str(raw.get('explanation', '')),
                            'steps': []}

    max_steps = int(policy.cloud.get('max_steps_per_plan', 10))
    if len(steps_raw) > max_steps:
        return False, f'Слишком много шагов в плане: {len(steps_raw)} > {max_steps}', None

    steps = []
    for i, step in enumerate(steps_raw):
        if not isinstance(step, dict):
            return False, f'Шаг {i}: должен быть объектом.', None
        tool = step.get('tool')
        if not isinstance(tool, str) or not tool:
            return False, f'Шаг {i}: нет поля tool.', None
        params = step.get('params', {})
        if not isinstance(params, dict):
            return False, f'Шаг {i}: params должен быть объектом.', None
        if tool == 'run_code':
            # специальный инструмент: выполняется ТОЛЬКО в песочнице
            if not policy.sandbox.get('enabled', True):
                return False, f'Шаг {i}: выполнение кода отключено политикой.', None
            code = params.get('content', '')
            if not isinstance(code, str) or not code.strip():
                return False, 'Шаг run_code: нужно поле content (код).', None
            max_chars = int(policy.sandbox.get('code_max_chars', 20000))
            if len(code) > max_chars:
                return False, f'Код слишком большой: {len(code)} > {max_chars}.', None
        elif not registry.get(tool):
            return False, f'Шаг {i}: неизвестный инструмент «{tool}».', None
        elif not policy.tool_enabled(tool):
            return False, f'Шаг {i}: инструмент «{tool}» отключён политикой.', None

        # Пути и URL (проверка на уровне валидатора плана)
        ok, reason, params = check_params(params, guard)
        if not ok:
            return False, f'Шаг {i}: {reason}', None

        steps.append({
            'tool': tool,
            'params': params,
            'reason': str(step.get('reason', '')),
            'confirm': bool(step.get('confirm', False)),
        })

    return True, None, {'explanation': str(raw.get('explanation', '')),
                        'steps': steps}


def plan_summary(plan):
    """Краткое человекочитаемое описание плана (для подтверждения)."""
    if not plan.get('steps'):
        return 'Без системных действий.'
    lines = []
    for i, step in enumerate(plan['steps'], 1):
        params = ', '.join(f'{k}={v}' for k, v in step['params'].items())
        lines.append(f'{i}. {step["tool"]}({params})')
    return '; '.join(lines)