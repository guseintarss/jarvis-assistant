"""YandexGPT — облачный провайдер (ключ YANDEX_API_KEY)."""

from jarvis import config

from jarvis.cloud.providers.base import OpenAICompatProvider


class YandexProvider(OpenAICompatProvider):
    """YandexGPT (OpenAI-совместимый endpoint, заголовок Api-Key)."""

    name = 'yandex'
    base_url = config.YANDEX_API_URL
    model = config.YANDEX_MODEL
    api_key = config.YANDEX_API_KEY
    auth_header = {'Api-Key': config.YANDEX_API_KEY} if config.YANDEX_API_KEY \
        else None