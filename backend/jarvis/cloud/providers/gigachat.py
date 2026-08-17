"""GigaChat (Сбер): OAuth2 client_credentials + OpenAI-совместимый API.

Транспорт отличается от остальных: сначала получаем JWT-токен по
client_id/client_secret (авторизация Basic), потом шлём запросы с
Bearer-токеном. Токен живёт ~30 минут и кэшируется в памяти процесса.
"""

import time

import requests

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class GigaChatProvider(OpenAICompatProvider):
    """GigaChat через OAuth2. Включается при наличии client_id/secret."""

    name = 'gigachat'
    base_url = config.GIGACHAT_API_URL
    model = config.GIGACHAT_MODEL
    api_key = None  # токен получаем сами, не из статичного ключа

    def __init__(self, log=None):
        super().__init__(log=log)
        self._token = None
        self._token_expires_at = 0.0

    def is_configured(self):
        return bool(config.GIGACHAT_CLIENT_ID and config.GIGACHAT_CLIENT_SECRET)

    # --------------------------- OAuth2 --------------------------------------

    def _obtain_token(self):
        """client_credentials -> JWT. Кэш в памяти до истечения срока."""
        if self._token and time.time() < self._token_expires_at:
            return True
        try:
            resp = requests.post(
                config.GIGACHAT_AUTH_URL,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json',
                    'Authorization': requests.auth._basic_auth_str(
                        config.GIGACHAT_CLIENT_ID,
                        config.GIGACHAT_CLIENT_SECRET),
                },
                data={'scope': 'GIGACHAT_API_PERS'},
                timeout=15)
            if resp.status_code != 200:
                return False
            data = resp.json()
            self._token = data.get('access_token', '')
            expires = int(data.get('expires_at', 0))  # unix-секунды
            self._token_expires_at = expires - 60 if expires else \
                time.time() + 1500
            return bool(self._token)
        except (requests.RequestException, ValueError):
            return False

    # --------------------------- запрос -------------------------------------

    def _headers(self):
        headers = {'Content-Type': 'application/json'}
        if self._token:
            headers['Authorization'] = f'Bearer {self._token}'
        return headers

    def request(self, payload):
        if not self._obtain_token():
            return False, '', 'не удалось получить OAuth-токен GigaChat'
        return super().request(payload)