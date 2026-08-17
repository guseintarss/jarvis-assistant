"""Планировщик проактивных действий (живёт в демоне).

Каждую PROACTIVE_POLL_INTERVAL_SEC секунд:
    * просроченные напоминания -> уведомление notify-send + пометка fired
      (просроченные при старте срабатывают сразу — напоминания переживают
      перезапуск демона);
каждые PROACTIVE_TRIGGER_INTERVAL_SEC секунд:
    * проверка триггеров (открыт VS Code и т.п.).

Работает в отдельном демоническом потоке; stop()/join() — корректное
завершение (вызывается при остановке D-Bus-цикла, в т.ч. по SIGTERM).
"""

import threading
import time

from jarvis import config
from jarvis.logger import get_logger

from jarvis.proactive.notify import notify as default_notify
from jarvis.proactive.reminders import ReminderStore
from jarvis.proactive.triggers import TriggerWatcher


class ProactiveScheduler:
    """Фоновый планировщик: напоминания + триггеры."""

    def __init__(self, store=None, notify_fn=None, triggers=None,
                 poll_interval=None, trigger_interval=None,
                 trigger_check_fn=None):
        """
        store     — ReminderStore (создаётся сам, если не передан);
        notify_fn — notify(title, text) (по умолчанию notify-send);
        triggers  — список триггеров (по умолчанию config.PROACTIVE_TRIGGERS);
        trigger_check_fn — для тестов: подмена проверки условий триггеров.
        """
        self._store = store or ReminderStore()
        self._owns_store = store is None
        self._notify = notify_fn or default_notify
        self._poll = poll_interval or config.PROACTIVE_POLL_INTERVAL_SEC
        self._triggers = triggers if triggers is not None \
            else config.PROACTIVE_TRIGGERS
        self._trigger_interval = trigger_interval or \
            config.PROACTIVE_TRIGGER_INTERVAL_SEC
        self._watcher = TriggerWatcher(
            self._triggers,
            on_fire=lambda t: self._fire_trigger(t),
            check_fn=trigger_check_fn)
        self._stop_event = threading.Event()
        self._thread = None
        self.stats = {'reminders_fired': 0, 'triggers_fired': 0,
                      'notify_failures': 0}

    # ------------------------- жизненный цикл -------------------------------

    def start(self):
        """Запускает фоновый поток планировщика (идемпотентно)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name='jarvis-proactive', daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        """Останавливает планировщик; ждёт завершения потока."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._owns_store:
            try:
                self._store.close()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------- основной цикл ------------------------------

    def _loop(self):
        last_triggers = 0.0
        while not self._stop_event.is_set():
            try:
                self._check_reminders()
            except Exception as exc:  # noqa: BLE001 — цикл не должен падать
                get_logger().event('proactive', error=f'reminders: {exc}')
            now = time.monotonic()
            if now - last_triggers >= self._trigger_interval:
                last_triggers = now
                try:
                    self._watcher.check_once()
                except Exception as exc:  # noqa: BLE001
                    get_logger().event('proactive', error=f'triggers: {exc}')
            self._stop_event.wait(self._poll)

    # ----------------------------- действия ---------------------------------

    def _check_reminders(self):
        for rid, when, text in self._store.due():
            ok = self._notify('Напоминание', f'«{text}» — {when[:16]}')
            if ok:
                self.stats['reminders_fired'] += 1
            else:
                self.stats['notify_failures'] += 1
            self._store.mark_fired(rid)
            get_logger().event('proactive', kind='reminder',
                                     id=rid, fired=True)

    def _fire_trigger(self, trigger):
        title = trigger.get('title') or 'Событие'
        text = trigger.get('text') or 'Что-то изменилось на машине.'
        ok = self._notify(title, text)
        if ok:
            self.stats['triggers_fired'] += 1
        else:
            self.stats['notify_failures'] += 1
        get_logger().event('proactive', kind='trigger', type=trigger.get('type'),
                                 fired=True)

    # ----------------------------- для статуса ------------------------------

    def pending(self):
        """Активные напоминания (id, when, text) — для D-Bus-статуса."""
        return self._store.upcoming()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()