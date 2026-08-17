"""DeepSeek — облачный провайдер (ключ DEEPSEEK_API_KEY)."""

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    """DeepSeek API (OpenAI-совместимый). Без ключа — не настроен."""

    name = 'deepseek'
    base_url = config.DEEPSEEK_BASE_URL
    model = config.DEEPSEEK_MODEL
    api_key = config.DEEPSEEK_API_KEY