"""Загрузка и валидация политики безопасности (policy.yaml).

Policy — центральный объект, через который модули спрашивают:
    • разрешён ли инструмент и какой у него риск;
    • нужно ли подтверждение для данного уровня риска;
    • какие корни разрешены, какие пути запрещены;
    • настройки sandbox и облака.

Если файл отсутствует или повреждён — используются встроенные безопасные
значения по умолчанию (никогда не падаем «наглухо», но и не ослабляем
политику: при ошибке YAML инструменты с риском high/medium отключаются).
"""

import os

import yaml

from jarvis import intents
from jarvis import logger

# Встроенные значения по умолчанию — копия backend/policy.yaml (безопасный минимум)
_DEFAULT_POLICY = {
    'allowed_roots': ['~'],
    'denylist_paths': [
        '~/.ssh', '~/.gnupg', '~/.aws', '~/.config', '~/.cache',
        '~/.bash_history', '~/.zsh_history', '~/.gitconfig',
        '/etc', '/usr', '/boot', '/proc', '/sys', '/dev', '/run', '/var',
        '/root', '/tmp',
    ],
    'index_roots': ['~/Projects', '~/Documents', '~/Downloads', '~/Desktop',
                    '~/Music', '~/Pictures', '~/Videos'],
    'index_extensions': ['.txt', '.md', '.json', '.py', '.sh', '.yaml',
                         '.yml', '.toml', '.ini', '.log'],
    'index_max_file_mb': 10,
    'confirmation': {'high': 'always', 'medium': 'never'},
    'tools': {},
    'sandbox': {'enabled': True, 'require_confirmation': True,
                'code_max_chars': 20000, 'time_limit_sec': 30,
                'memory_limit_mb': 512, 'network': False,
                'output_max_bytes': 65536},
    'cloud': {'enabled': True, 'max_steps_per_plan': 10, 'timeout_sec': 90,
              'forbidden_fields': ['command', 'shell', 'script', 'exec',
                                   'subprocess', 'os_system']},
    'logging': {'jsonl': True,
                'log_dir': '~/.local/share/jarvis-assistant/logs'},
}


def _expand(path):
    """Раскрывает '~' и приводит к абсолютному виду."""
    return os.path.abspath(os.path.expanduser(path))


def _deep_merge(base, override):
    """Рекурсивное слияние словарей (override поверх base)."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _deep_merge(base[key], value)
        else:
            out[key] = value
    return out


class Policy:
    """Политика безопасности ассистента."""

    def __init__(self, data=None, source='defaults'):
        d = _deep_merge(_DEFAULT_POLICY, data or {})
        self.source = source
        self._raw = d
        self.allowed_roots = [_expand(p) for p in d['allowed_roots']]
        self.denied_paths = [_expand(p) for p in d['denylist_paths']]
        self.index_roots = [_expand(p) for p in d['index_roots']]
        self.index_extensions = [e.lower() for e in d['index_extensions']]
        self.index_max_bytes = int(d.get('index_max_file_mb', 10)) * 1024 * 1024
        self.confirmation = d['confirmation']
        self.sandbox = d['sandbox']
        self.cloud = d['cloud']
        self.logging = d['logging']
        self._tools = d['tools']

    # ------------------------- инструменты --------------------------------

    def tool_spec(self, name):
        """Настройка инструмента из policy.yaml (может быть None)."""
        return self._tools.get(name)

    def tool_enabled(self, name):
        spec = self._tools.get(name)
        if spec is None:
            return name in intents.INTENTS  # по умолчанию инструмент включён
        return bool(spec.get('enabled', True))

    def tool_risk(self, name):
        """Риск инструмента: переопределение из policy или дефолт из intents."""
        spec = self._tools.get(name)
        if spec and spec.get('risk'):
            risk = str(spec['risk']).lower()
            if risk in (intents.RISK_LOW, intents.RISK_MEDIUM, intents.RISK_HIGH):
                return risk
        intent = intents.INTENTS.get(name)
        return intent.risk if intent else intents.RISK_MEDIUM

    # ------------------------- подтверждения --------------------------------

    def needs_confirmation(self, risk):
        """Нужно ли подтверждение для уровня риска (high/medium)."""
        mode = self.confirmation.get(risk, 'never')
        return mode == 'always'

    # ------------------------- пути -----------------------------------------

    def is_denied(self, path):
        """Запрещён ли путь (равен или лежит внутри denylist-каталога)."""
        p = _expand(path)
        return any(p == d or p.startswith(d + os.sep) for d in self.denied_paths)

    def is_allowed(self, path):
        """Разрешён ли путь: внутри allowed_roots и не в denylist."""
        p = _expand(path)
        inside = any(p == r or p.startswith(r + os.sep) for r in self.allowed_roots)
        return inside and not self.is_denied(p)

    # ------------------------- прочее ----------------------------------------

    def set_tool(self, name, enabled=None, risk=None):
        """Обновляет настройку инструмента в памяти (для веб-настроек).

        Работает на живом объекте политики: включение/выключение и риск
        применяются сразу. Возвращает True при успехе.
        """
        if enabled is not None and not isinstance(enabled, bool):
            return False
        if risk is not None and risk not in (intents.RISK_LOW,
                                             intents.RISK_MEDIUM,
                                             intents.RISK_HIGH):
            return False
        spec = dict(self._tools.get(name) or {})
        if enabled is not None:
            spec['enabled'] = enabled
        if risk is not None:
            spec['risk'] = risk
        if not spec:
            return False
        self._tools[name] = spec
        return True

    @classmethod
    def load(cls, path):
        """Загружает политику из YAML. При ошибке — defaults + отключение
        рискованных инструментов (безопаснее упасть в консервативный режим)."""
        if not os.path.isfile(path):
            logger.get_logger().event('policy_missing', path=path)
            return cls(source=f'missing:{path}')
        try:
            with open(path, encoding='utf-8') as f:
                raw = yaml.safe_load(f) or {}
            data = raw.get('policy', raw)
            return cls(data=data, source=path)
        except Exception as exc:  # YAML-ошибка или неверная структура
            logger.get_logger().event('policy_error', path=path,
                                       error=str(exc))
            conservative = dict(_DEFAULT_POLICY)
            conservative['tools'] = {name: {'enabled': False}
                                     for name in intents.INTENTS
                                     if intents.INTENTS[name].risk != intents.RISK_LOW}
            return cls(data=conservative, source=f'fallback:{path}')