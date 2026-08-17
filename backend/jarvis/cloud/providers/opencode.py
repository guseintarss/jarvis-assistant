"""opencode.ai/zen — бесплатный OpenAI-совместимый шлюз (последняя попытка).

Не требует ключа и регистрации; при недоступности остальной цепочки
всегда есть рабочий вариант (когда шлюз не перегружен).
"""

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class OpenCodeProvider(OpenAICompatProvider):
    """Шлюз opencode.ai/zen (без ключа по умолчанию)."""

    name = 'opencode'
    base_url = config.CLOUD_BASE_URL
    model = config.CLOUD_MODEL
    api_key = config.CLOUD_API_KEY or None  # ключ не обязателен