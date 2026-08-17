"""JSONL-логирование всех действий ассистента.

Каждое событие — одна строка JSON вида:
    {"ts": "2026-08-16T12:00:00.123456+03:00", "kind": "executed", ...поля}

Безопасность: перед записью значения проходят редэкцию — строки вида
`password=...`, `token=...`, `api_key=...` и любые ключи с такими именами
заменяются на маркер. Приватные файлы ассистент не читает вовсе (см. security),
поэтому в логи не попадают ни ключи, ни содержимое ~/.ssh и т.п.
"""

import datetime
import json
import os
import re

# Паттерны секретов в произвольных строках (значение после имени ключа)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|token|password|passwd|authorization|bearer)"
    r"['\"]?\s*[:=]\s*['\"]?[^\s,'\"]{4,}")

# Имена JSON-ключей, значения которых всегда редэктируются
_SECRET_KEYS = frozenset({
    'api_key', 'apikey', 'secret', 'password', 'passwd', 'token',
    'authorization', 'access_token', 'refresh_token', 'private_key',
})


def redact(value):
    """Заменяет секреты в строках и рекурсивно в dict/list."""
    if isinstance(value, dict):
        return {k: ('***REDACTED***' if k.lower() in _SECRET_KEYS
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(r'\1=***REDACTED***', value)
    return value


class JsonlLogger:
    """Потокобезопасный JSONL-логгер с ротацией по дням."""

    def __init__(self, log_dir, enabled=True):
        self.log_dir = log_dir
        self.enabled = enabled
        self._lock = __import__('threading').Lock()
        self._fh = None
        self._day = None
        if enabled:
            os.makedirs(log_dir, exist_ok=True)

    def _open(self):
        today = datetime.date.today().isoformat()
        if self._fh is not None and self._day == today:
            return self._fh
        if self._fh is not None:
            self._fh.close()
        path = os.path.join(self.log_dir, f'jarvis-{today}.jsonl')
        self._fh = open(path, 'a', encoding='utf-8')
        self._day = today
        return self._fh

    def event(self, kind, **fields):
        """Записывает событие {kind: ..., **fields} одной строкой JSON."""
        if not self.enabled:
            return
        record = {
            'ts': datetime.datetime.now().astimezone().isoformat(),
            'kind': kind,
            **redact(fields),
        }
        try:
            with self._lock:
                self._open().write(json.dumps(record, ensure_ascii=False) + '\n')
                self._fh.flush()
        except OSError:
            pass  # логирование не должно ломать работу ассистента

    def close(self):
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


_default_logger = None


def get_logger(log_dir=None, enabled=None):
    """Глобальный логгер (синглтон)."""
    global _default_logger
    if _default_logger is None:
        _default_logger = JsonlLogger(
            log_dir or os.path.expanduser('~/.local/share/jarvis-assistant/logs'),
            enabled=True if enabled is None else enabled)
    return _default_logger