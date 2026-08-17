"""История действий для undo: что и с какими параметрами выполнялось.

Каждое выполненное действие инструмента (успешное или нет) сохраняется
в таблицу actions. undo_last() возвращает последнее УСПЕШНОЕ действие —
пайплайн может откатить его (например, restore для move_to_trash).

Запись действий идёт из Executor'а (единственная точка выполнения),
а не из pipeline — чтобы в историю попадали и облачные планы.
"""

import json

from jarvis import logger


class ActionHistory:
    """Стек выполненных действий (SQLite, таблица actions)."""

    MAX_KEPT = 100

    def __init__(self, conn, lock, log=None):
        self._conn = conn
        self._lock = lock
        self.log = log or logger.get_logger()

    def push(self, tool, params=None, ok=True, message=''):
        """Сохраняет запись о выполненном действии."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO actions (ts, tool, params, ok, message) '
                'VALUES (?, ?, ?, ?, ?)',
                (logger.datetime_now(), tool,
                 json.dumps(params or {}, ensure_ascii=False),
                 1 if ok else 0, (message or '')[:500]))
            self._conn.execute(
                'DELETE FROM actions WHERE id NOT IN '
                '(SELECT id FROM actions ORDER BY id DESC LIMIT ?)',
                (self.MAX_KEPT,))
            self._conn.commit()

    def last(self, n=10):
        """Последние n записей [{tool, params, ok, message, ts}] (старые->новые)."""
        cur = self._conn.execute(
            'SELECT tool, params, ok, message, ts FROM actions '
            'ORDER BY id DESC LIMIT ?', (n,))
        rows = []
        for tool, params, ok, message, ts in reversed(cur.fetchall()):
            try:
                params = json.loads(params)
            except (TypeError, ValueError):
                params = {}
            rows.append({'tool': tool, 'params': params,
                         'ok': bool(ok), 'message': message, 'ts': ts})
        return rows

    def undo_last(self):
        """Возвращает последнее УСПЕШНОЕ действие (и удаляет его из стека).

        None — если откатывать нечего. Пайплайн решает, как откатить
        (инструмент, который умеет undo, например restore_from_trash).
        """
        with self._lock:
            cur = self._conn.execute(
                'SELECT id, tool, params, message FROM actions '
                'WHERE ok = 1 ORDER BY id DESC LIMIT 1')
            row = cur.fetchone()
            if row is None:
                return None
            action_id, tool, params, message = row
            self._conn.execute('DELETE FROM actions WHERE id=?', (action_id,))
            self._conn.commit()
            try:
                params = json.loads(params)
            except (TypeError, ValueError):
                params = {}
            return {'tool': tool, 'params': params, 'message': message}