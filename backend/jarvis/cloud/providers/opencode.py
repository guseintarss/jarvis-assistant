"""opencode.ai/zen — бесплатные OpenAI-совместимые модели (облако по умолчанию).

Не требует ключа и регистрации. build_chain подставляет по одному
провайдеру на каждую бесплатную модель (config.OPENCODE_FREE_MODELS):
цепочка пробует их по очереди, пока одна не ответит.
"""

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class OpenCodeProvider(OpenAICompatProvider):
    """Шлюз opencode.ai/zen (без ключа по умолчанию).

    model=None -> config.CLOUD_MODEL (одна модель); при создании через
    build_chain модель задаётся явно, имя провайдера — 'opencode:<модель>'.
    """

    name = 'opencode'
    base_url = config.CLOUD_BASE_URL
    model = config.CLOUD_MODEL
    api_key = config.CLOUD_API_KEY or None  # ключ не обязателен

    def __init__(self, model=None, log=None):
        super().__init__(log)
        if model:
            self.model = model
            self.name = f'opencode:{model}'