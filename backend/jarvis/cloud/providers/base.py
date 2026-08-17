"""Базовые классы провайдеров облачных LLM (ЧАСТЬ 5).

Provider — интерфейс: провайдер получает payload (messages/temperature/
max_tokens БЕЗ model — модель вставляет сам) и возвращает
(ok, content, error). Секреты провайдеров — только из окружения.

OpenAICompatProvider — общий транспорт для OpenAI-совместимых API
(URL + Authorization-заголовок + 429-ретраи). Все провайдеры в
цепочке наследуют его и задают свою конфигурацию.
"""

import time

import requests

from jarvis import config
from jarvis import logger


class Provider:
    """Интерфейс провайдера. name — уникальный ключ для роутера."""

    name = 'base'

    def is_configured(self):
        """Готов ли провайдер работать (ключи/URL заданы)."""
        raise NotImplementedError

    def request(self, payload):
        """payload -> (ok, content, error). content — сырой текст модели."""
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    """Провайдер с OpenAI-совместимым /chat/completions транспортом."""

    #: путь к API (без /chat/completions)
    base_url = ''
    #: имя модели
    model = ''
    #: готовность определяется наличием ключа; None = ключ не обязателен
    api_key = None
    #: дополнительный заголовок авторизации, например {'Api-Key': ...}
    auth_header = None
    #: локальный сервис (Ollama) — таймаут соединения мал, чтобы не ждать
    local = False

    def __init__(self, log=None):
        self.log = log or logger.get_logger()
        self._session = requests.Session()

    def is_configured(self):
        if self.api_key is None:
            return bool(self.base_url)
        return bool(self.base_url and self.api_key)

    def _headers(self):
        headers = {'Content-Type': 'application/json'}
        if self.auth_header:
            headers.update(self.auth_header)
        elif self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers

    def _timeout(self):
        if self.local:
            return min(config.OLLAMA_TIMEOUT_SEC, config.CLOUD_TIMEOUT_SEC)
        return config.CLOUD_TIMEOUT_SEC

    def request(self, payload):
        """POST /chat/completions с ретраями на 429. (ok, content, error)."""
        body = {**payload, 'model': self.model}
        last_error = 'провайдер не ответил'
        for attempt in range(config.CLOUD_RETRIES + 1):
            try:
                resp = self._session.post(
                    f'{self.base_url}/chat/completions',
                    headers=self._headers(), json=body,
                    timeout=self._timeout())
                if resp.status_code == 429:
                    last_error = 'перегружен (429)'
                    if attempt < config.CLOUD_RETRIES:
                        time.sleep(config.CLOUD_RETRY_DELAY_SEC)
                    continue
                if resp.status_code == 401:
                    return False, '', 'отказано в доступе (401): проверьте ключ'
                resp.raise_for_status()
                content = self._extract_content(resp.json())
                if not content:
                    last_error = 'пустой ответ модели'
                    continue
                return True, content, ''
            except requests.RequestException as exc:
                last_error = f'ошибка сети: {exc}'
            except (ValueError, KeyError, TypeError) as exc:
                last_error = f'неожиданный ответ: {exc}'
        return False, '', last_error

    @staticmethod
    def _extract_content(data):
        """content модели; reasoning_content игнорируется («мысли»)."""
        choice = data['choices'][0]
        message = choice.get('message', {})
        return message.get('content') or ''