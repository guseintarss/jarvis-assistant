"""Защита от превышения лимитов: rate limiter + circuit breaker (ЧАСТЬ 5).

RateLimiter хранит состояние по имени провайдера:
    • окно в минуту — не больше max_per_minute запросов;
    • circuit breaker — после RATE_LIMIT_COOLDOWN_FAILURES подряд неудач
      провайдер «остывает» RATE_LIMIT_COOLDOWN_SEC и пропускается
      роутером; успешный запрос сбрасывает счётчик неудач.

Потокобезопасен (lock), не зависит от времени реальных запросов —
в тестах время можно подменить.
"""

import threading
import time

from jarvis import config


class RateLimiter:
    """Токен-бакет на минуту + cooldown после серии неудач, на провайдера."""

    def __init__(self, max_per_minute=None, cooldown_sec=None,
                 cooldown_failures=None, now_fn=None):
        self.max_per_minute = max_per_minute or config.RATE_LIMIT_PER_MINUTE
        self.cooldown_sec = cooldown_sec or config.RATE_LIMIT_COOLDOWN_SEC
        self.cooldown_failures = cooldown_failures or \
            config.RATE_LIMIT_COOLDOWN_FAILURES
        self._now = now_fn or time.monotonic
        self._lock = threading.Lock()
        self._state = {}  # provider -> {window_start, count, failures}

    def _state_for(self, name):
        if name not in self._state:
            self._state[name] = {'window_start': self._now(), 'count': 0,
                                 'failures': 0, 'cooldown_since': None}
        return self._state[name]

    def _reset_window(self, st):
        st['window_start'] = self._now()
        st['count'] = 0

    def allow(self, name):
        """Можно ли сейчас отправить запрос провайдеру name."""
        with self._lock:
            st = self._state_for(name)
            if self._now() - st['window_start'] >= 60:
                self._reset_window(st)
            if st['failures'] >= self.cooldown_failures:
                # circuit breaker: ещё не время возвращаться
                if st['cooldown_since'] is not None and \
                        self._now() - st['cooldown_since'] < self.cooldown_sec:
                    return False
                # cooldown прошёл — пробуем снова
                st['failures'] = 0
                st['cooldown_since'] = None
            if st['count'] >= self.max_per_minute:
                return False
            st['count'] += 1
            return True

    def success(self, name):
        """Успешный ответ: сбрасываем счётчик неудач."""
        with self._lock:
            st = self._state_for(name)
            st['failures'] = 0

    def failure(self, name):
        """Неудача: наращиваем счётчик для circuit breaker."""
        with self._lock:
            st = self._state_for(name)
            st['failures'] += 1
            # при переполнении cooldown отсчитывается от первой неудачи
            if st['failures'] == self.cooldown_failures:
                st['cooldown_since'] = self._now()

    def status(self, name):
        """Текущее состояние (для диагностики/статуса)."""
        with self._lock:
            st = self._state_for(name)
            return dict(st)