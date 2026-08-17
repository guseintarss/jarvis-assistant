"""Провайдеры облачных LLM в цепочке роутера (ЧАСТЬ 5).

    ollama     — локальная Ollama (первая попытка, без сети);
    opencode   — БЕСПЛАТНЫЕ модели шлюза opencode.ai/zen, по одной на
                 модель (config.OPENCODE_FREE_MODELS): цепочка пробует
                 их по очереди — пока одна не ответит;
    deepseek   — DeepSeek API (ключ DEEPSEEK_API_KEY);
    gigachat   — GigaChat (client_id/client_secret, OAuth2);
    yandex     — YandexGPT (ключ YANDEX_API_KEY).

Провайдеры без ключей остаются в цепочке, но роутер пропускает их
на этапе is_configured() — не тратится ни одного сетевого запроса.
"""

from jarvis import config


def build_chain():
    """Цепочка провайдеров по порядку: локальная Ollama -> все бесплатные
    модели opencode -> платные (если заданы ключи)."""
    from jarvis.cloud.providers.ollama import OllamaProvider
    from jarvis.cloud.providers.deepseek import DeepSeekProvider
    from jarvis.cloud.providers.gigachat import GigaChatProvider
    from jarvis.cloud.providers.yandex import YandexProvider
    from jarvis.cloud.providers.opencode import OpenCodeProvider

    providers = [OllamaProvider()]
    free_models = config.OPENCODE_FREE_MODELS or [config.CLOUD_MODEL]
    providers.extend(OpenCodeProvider(model=m) for m in free_models)
    providers += [
        DeepSeekProvider(),
        GigaChatProvider(),
        YandexProvider(),
    ]
    return providers