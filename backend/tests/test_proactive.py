"""Тесты ЧАСТИ 3: проактивные действия (напоминания, планировщик, триггеры).

Напоминания проверяются на временных БД; уведомления подменяются
коллектором; условия триггеров — фиктивными check_fn (реальный /proc
не сканируется).
"""

import datetime
import os
import tempfile
import time
import unittest

from jarvis.proactive import reminders as reminders_mod
from jarvis.proactive.reminders import ReminderStore
from jarvis.proactive.scheduler import ProactiveScheduler
from jarvis.proactive.triggers import TriggerWatcher


class NotifyCollector:
    """Подмена notify-send: собирает (title, text) и записывает в список."""

    def __init__(self):
        self.calls = []

    def __call__(self, title, text):
        self.calls.append((title, text))
        return True


class TestReminderStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ReminderStore(os.path.join(self.tmp, 'reminders.db'))

    def tearDown(self):
        self.store.close()
        os.unlink(self.store.db_path)

    def _iso(self, seconds_from_now):
        return (datetime.datetime.now()
                + datetime.timedelta(seconds=seconds_from_now)).isoformat()

    def test_add_and_upcoming(self):
        rid = self.store.add(self._iso(3600), 'полить цветы')
        rows = self.store.upcoming()
        self.assertEqual([r[0] for r in rows], [rid])
        self.assertEqual(rows[0][2], 'полить цветы')

    def test_due_filters_by_time(self):
        past = self.store.add(self._iso(-10), 'уже пора')
        future = self.store.add(self._iso(3600), 'ещё рано')
        due = self.store.due()
        self.assertEqual([r[0] for r in due], [past])
        self.assertNotIn(future, [r[0] for r in due])

    def test_mark_fired(self):
        rid = self.store.add(self._iso(-10), 'тест')
        self.store.mark_fired(rid)
        self.assertEqual(self.store.upcoming(), [])
        self.assertEqual(self.store.due(), [])

    def test_delete_and_clear(self):
        a = self.store.add(self._iso(-10), 'a')
        b = self.store.add(self._iso(-10), 'b')
        self.store.delete(a)
        self.assertEqual([r[0] for r in self.store.upcoming()], [b])
        self.store.clear()
        self.assertEqual(self.store.upcoming(), [])

    def test_bad_iso_skipped(self):
        self.store.add('не-дата', 'мусор')
        self.assertEqual(self.store.due(), [])


class TestProactiveScheduler(unittest.TestCase):
    def test_fires_due_reminder_once(self):
        tmp = tempfile.mkdtemp()
        store = ReminderStore(os.path.join(tmp, 'r.db'))
        rid = store.add((datetime.datetime.now()
                         - datetime.timedelta(seconds=5)).isoformat(),
                        'проверить окна')
        collector = NotifyCollector()
        sched = ProactiveScheduler(store=store, notify_fn=collector,
                                   triggers=[], poll_interval=0.05)
        sched.start()
        try:
            deadline = time.time() + 5
            while not collector.calls and time.time() < deadline:
                time.sleep(0.05)
        finally:
            sched.stop()
        self.assertEqual(len(collector.calls), 1)
        self.assertIn('«проверить окна»', collector.calls[0][1])
        self.assertTrue(collector.calls[0][0], 'Напоминание')
        self.assertTrue(store.upcoming() == [])
        self.assertEqual(sched.stats['reminders_fired'], 1)
        os.unlink(store.db_path)

    def test_future_reminder_not_fired(self):
        tmp = tempfile.mkdtemp()
        store = ReminderStore(os.path.join(tmp, 'r.db'))
        store.add((datetime.datetime.now()
                   + datetime.timedelta(hours=2)).isoformat(), 'позже')
        collector = NotifyCollector()
        sched = ProactiveScheduler(store=store, notify_fn=collector,
                                   triggers=[], poll_interval=0.05)
        sched.start()
        time.sleep(0.2)
        sched.stop()
        self.assertEqual(collector.calls, [])
        self.assertEqual(len(store.upcoming()), 1)
        os.unlink(store.db_path)

    def test_stop_is_idempotent(self):
        collector = NotifyCollector()
        sched = ProactiveScheduler(store=ReminderStore(
            os.path.join(tempfile.mkdtemp(), 'r.db')),
            notify_fn=collector, triggers=[], poll_interval=0.05)
        sched.stop()
        sched.stop()
        self.assertFalse(sched.is_running())


class TestTriggerWatcher(unittest.TestCase):
    def test_fires_on_rising_edge_only(self):
        fired = []
        seq = iter([False, True, True, False, True])
        watcher = TriggerWatcher(
            [{'type': 'process', 'name': 'code'}],
            on_fire=lambda t: fired.append(t.get('name')),
            check_fn=lambda t: next(seq))
        watcher.check_once()   # False
        watcher.check_once()   # True  -> fire
        watcher.check_once()   # True  -> нет (уже активно)
        watcher.check_once()   # False -> сброс
        watcher.check_once()   # True  -> fire снова
        self.assertEqual(fired, ['code', 'code'])

    def test_file_trigger(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'flag.txt')
        fired = []
        watcher = TriggerWatcher(
            [{'type': 'file', 'path': path}],
            on_fire=lambda t: fired.append(True))
        watcher.check_once()
        self.assertEqual(fired, [])
        open(path, 'w').close()
        watcher.check_once()
        self.assertEqual(fired, [True])
        watcher.check_once()
        self.assertEqual(fired, [True])  # фронт уже отработан

    def test_bad_check_does_not_raise(self):
        watcher = TriggerWatcher(
            [{'type': 'process', 'name': 'x'}],
            on_fire=lambda t: None,
            check_fn=lambda t: (_ for _ in ()).throw(RuntimeError('boom')))
        watcher.check_once()  # не должно упасть


if __name__ == '__main__':
    unittest.main()