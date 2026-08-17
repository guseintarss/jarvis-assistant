"""Ollama — локальная LLM (первая попытка в цепочке, без сети)."""

import requests

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class OllamaProvider(OpenAICompatProvider):
    """Локальная Ollama: OpenAI-совместимый API на 127.0.0.1:11434.

    OpenAI-эндпоинт Ollama живёт на /v1/chat/completions (корень
    /chat/completions — это 404), поэтому base_url дополняется /v1.
    Для локальной генерации план компактнее: max_tokens ограничиваем
    600 (плану больше не нужно), keep_alive держит модель «тёплой»
    между запросами — повторные вызовы значительно быстрее.
    """

    name = 'ollama'
    base_url = config.OLLAMA_URL.rstrip('/') + '/v1'
    model = config.OLLAMA_MODEL
    api_key = None  # ключ не нужен
    local = True
    plan_max_tokens = 600

    def request(self, payload):
        body = {**payload, 'model': self.model}
        if body.get('max_tokens', 0) > self.plan_max_tokens:
            body['max_tokens'] = self.plan_max_tokens
        body['keep_alive'] = '5m'
        return self._post(body)

    def _post(self, body):
        """POST без ретраев на 429 — Ollama не throttles (локальная)."""
        last_error = 'ollama не ответил'
        try:
            resp = self._session.post(
                f'{self.base_url}/chat/completions',
                headers=self._headers(), json=body,
                timeout=self._timeout())
            resp.raise_for_status()
            content = self._extract_content(resp.json())
            if not content:
                return False, '', 'пустой ответ модели'
            return True, content, ''
        except requests.RequestException as exc:
            last_error = f'ошибка сети: {exc}'
        except (ValueError, KeyError, TypeError) as exc:
            last_error = f'неожиданный ответ: {exc}'
        return False, '', last_error