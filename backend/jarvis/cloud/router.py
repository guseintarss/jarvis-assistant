"""CloudRouter: цепочка провайдеров с fallback (ЧАСТЬ 5).

Порядок попыток: ollama (локально) -> deepseek -> gigachat -> yandex
-> opencode. Роутер:
    • пропускает не настроенных провайдеров (build_chain);
    • перед запросом спрашивает RateLimiter (минутный лимит + cooldown
      после серии неудач — circuit breaker);
    • при неудаче переходит к следующему провайдеру и логирует цепочку;
    • успешный ответ сбрасывает счётчик неудач провайдера.

attempts() отдаёт результаты по одному — CloudLLM валидирует план и,
если план отвергнут, запрашивает следующего провайдера.
"""

from jarvis import logger

from jarvis.cloud.rate_limiter import RateLimiter
from jarvis.cloud.providers import build_chain


class CloudRouter:
    """Упорядоченная цепочка провайдеров с rate limit и cooldown."""

    def __init__(self, providers=None, limiter=None, log=None):
        self.providers = providers if providers is not None else build_chain()
        self.limiter = limiter or RateLimiter()
        self.log = log or logger.get_logger()
        self.stats = {p.name: {'ok': 0, 'fail': 0, 'skipped': 0}
                      for p in self.providers}

    def chain(self):
        """Имена провайдеров по порядку."""
        return [p.name for p in self.providers]

    def attempts(self, payload):
        """Генератор: по одному провайдеру до первого успеха.

        yield (provider_name, ok, content, error). Исчерпав цепочку,
        останавливается (все провайдеры неуспешны).
        """
        for provider in self.providers:
            name = provider.name
            try:
                if not provider.is_configured():
                    self.stats[name]['skipped'] += 1
                    self.log.event('cloud_provider', provider=name,
                                   action='skipped', reason='not configured')
                    continue
                if not self.limiter.allow(name):
                    self.stats[name]['skipped'] += 1
                    self.log.event('cloud_provider', provider=name,
                                   action='skipped', reason='rate limit')
                    continue
                ok, content, error = provider.request(payload)
            except Exception as exc:  # noqa: BLE001 — провайдер не должен
                ok, content, error = False, '', f'сбой провайдера: {exc}'
            if ok:
                self.limiter.success(name)
                self.stats[name]['ok'] += 1
                self.log.event('cloud_provider', provider=name, action='ok')
                yield name, True, content, ''
                return
            self.limiter.failure(name)
            self.stats[name]['fail'] += 1
            self.log.event('cloud_provider', provider=name, action='fail',
                           error=error[:300])
            yield name, False, '', error

    def request(self, payload):
        """Первый успех в цепочке -> (ok, content, error, provider).

        ok=False: error — сообщение последнего провайдера.
        """
        result = None
        for provider_name, ok, content, error in self.attempts(payload):
            result = (ok, content, error, provider_name)
            if ok:
                return result
        return result or (False, '', 'нет настроенных провайдеров', '')