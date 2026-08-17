"""Провайдеры облачных LLM в цепочке роутера (ЧАСТЬ 5).

    ollama    — локальная Ollama (первая попытка, без сети);
    deepseek  — DeepSeek API (ключ DEEPSEEK_API_KEY);
    gigachat  — GigaChat (client_id/client_secret, OAuth2);
    yandex    — YandexGPT (ключ YANDEX_API_KEY);
    opencode  — бесплатный OpenAI-совместимый шлюз opencode.ai/zen.

Провайдеры без ключей остаются в цепочке, но роутер пропускает их
на этапе is_configured() — не тратится ни одного сетевого запроса.
"""

from jarvis import config


def build_chain():
    """Цепочка провайдеров по порядку (задаётся в ТЗ ЧАСТИ 5)."""
    from jarvis.cloud.providers.ollama import OllamaProvider
    from jarvis.cloud.providers.deepseek import DeepSeekProvider
    from jarvis.cloud.providers.gigachat import GigaChatProvider
    from jarvis.cloud.providers.yandex import YandexProvider
    from jarvis.cloud.providers.opencode import OpenCodeProvider

    providers = [
        OllamaProvider(),
        DeepSeekProvider(),
        GigaChatProvider(),
        YandexProvider(),
        OpenCodeProvider(),
    ]
    return providers