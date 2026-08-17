"""Хранилище напоминаний (SQLite) — единый источник правды.

Пишут инструменты (jarvis/tools/time.py, интент set_reminder), читает
планировщик (jarvis/proactive/scheduler.py). Просроченные напоминания
срабатывают сразу после старта демона — перезапуск их не теряет.
"""

import datetime
import os
import sqlite3

from jarvis import config


class ReminderStore:
    """Хранилище напоминаний (SQLite, WAL)."""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.REMINDERS_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)),
                    exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                when_ts    TEXT NOT NULL,     -- ISO, момент срабатывания
                text       TEXT NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                fired      INTEGER NOT NULL DEFAULT 0,
                created_ts TEXT NOT NULL
            );
        ''')
        self._conn.commit()

    def add(self, when_ts, text):
        cur = self._conn.execute(
            'INSERT INTO reminders (when_ts, text, created_ts) VALUES (?, ?, ?)',
            (when_ts, text[:500], datetime.datetime.now().isoformat()))
        self._conn.commit()
        return cur.lastrowid

    def upcoming(self):
        """Активные (не сработавшие) напоминания, ближайшие первыми."""
        cur = self._conn.execute(
            'SELECT id, when_ts, text FROM reminders WHERE fired = 0 '
            'ORDER BY when_ts')
        return cur.fetchall()

    def due(self, now=None):
        """Напоминания, момент которых наступил (ISO now или текущее время).

        Возвращает список (id, when_ts, text) в порядке времени.
        """
        now = now or datetime.datetime.now()
        rows = []
        for rid, when, text in self.upcoming():
            try:
                due_ts = datetime.datetime.fromisoformat(when)
            except ValueError:
                continue
            if due_ts <= now:
                rows.append((rid, when, text))
        return rows

    def mark_fired(self, reminder_id):
        self._conn.execute('UPDATE reminders SET fired = 1 WHERE id = ?',
                           (reminder_id,))
        self._conn.commit()

    def delete(self, reminder_id):
        self._conn.execute('DELETE FROM reminders WHERE id = ?',
                           (reminder_id,))
        self._conn.commit()

    def clear(self):
        self._conn.execute('DELETE FROM reminders')
        self._conn.commit()

    def close(self):
        self._conn.close()