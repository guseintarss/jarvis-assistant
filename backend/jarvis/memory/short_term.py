"""Краткосрочная память: последние 20 реплик диалога.

Реплики хранятся в SQLite (таблица turns), чтобы контекст переживал
перезапуск демона; deque(maxlen=20) — быстрый кэш для обращений без
лишнего запроса к БД. Роль реплики — 'user' или 'assistant'.

Также здесь решается задача «открой второй файл»: из последнего ответа
ассистента извлекаются пронумерованные пункты (1) ..., 2) ...) и по
порядковому слову («второй», «третий») возвращается нужный элемент.
"""

import re
from collections import deque

from jarvis import logger

# Порядковые слова -> индекс в пронумерованном списке (0-based);
# «последний» — отдельно (индекс -1)
_ORDINALS = {
    'перв': 0,
    'втор': 1,
    'трет': 2,
    'четверт': 3,
    'пят': 4,
    'шест': 5,
    'седьм': 6,
    'восьм': 7,
    'девят': 8,
    'десят': 9,
}

# Пронумерованные пункты в тексте: "1) файл.pdf" / "2. договор"
_ITEM_RE = re.compile(r'^\s*\d+\s*[\)\.]\s+(.+?)\s*$', re.MULTILINE)


class ShortTermMemory:
    """Кольцевой буфер последних реплик (user/assistant)."""

    MAX_TURNS = 20

    def __init__(self, conn, lock, log=None):
        self._conn = conn
        self._lock = lock
        self.log = log or logger.get_logger()
        self._cache = deque(maxlen=self.MAX_TURNS)
        self._load()

    # --------------------------- загрузка ----------------------------------

    def _load(self):
        """Читает последние MAX_TURNS реплик из БД в кэш."""
        cur = self._conn.execute(
            'SELECT role, text FROM turns ORDER BY id DESC LIMIT ?',
            (self.MAX_TURNS,))
        for role, text in reversed(cur.fetchall()):
            self._cache.append((role, text))

    # --------------------------- запись ------------------------------------

    def add(self, role, text):
        """Добавляет реплику; лишние старые реплики удаляются из БД."""
        text = (text or '').strip()
        if not text:
            return
        with self._lock:
            self._conn.execute(
                'INSERT INTO turns (ts, role, text) VALUES (?, ?, ?)',
                (logger.datetime_now(), role, text[:4000]))
            # оставляем ровно MAX_TURNS последних строк
            self._conn.execute(
                'DELETE FROM turns WHERE id NOT IN '
                '(SELECT id FROM turns ORDER BY id DESC LIMIT ?)',
                (self.MAX_TURNS,))
            self._conn.commit()
        self._cache.append((role, text))

    # --------------------------- чтение ------------------------------------

    def turns(self):
        """Список (role, text) последних реплик, от старых к новым."""
        return list(self._cache)

    def last_assistant(self):
        """Текст последнего ответа ассистента (или '')."""
        for role, text in reversed(self._cache):
            if role == 'assistant':
                return text
        return ''

    def recent_user_texts(self, n=5):
        """Последние n реплик пользователя (для подсказок/промпта)."""
        return [text for role, text in self._cache
                if role == 'user'][-n:]

    # --------------------------- ссылки ------------------------------------

    def resolve_ordinal(self, index):
        """Элемент под номером index из списка в последнем ответе (или None).

        index — 0-based; -1 означает «последний пункт».
        Пункты распознаются как "1) текст" / "2. текст" в конце ответа.
        """
        last = self.last_assistant()
        if not last:
            return None
        items = [m.group(1) for m in _ITEM_RE.finditer(last)]
        if not items:
            return None
        try:
            return items[index]
        except IndexError:
            return None

    @staticmethod
    def ordinal_index(text):
        """Порядковое слово -> индекс ('второй' -> 1), иначе None.

        Поддерживает «первый…десятый» и «последний» (-> -1).
        """
        t = (text or '').lower()
        m = re.search(r'\b(перв|втор|трет|четверт|пят|шест|седьм|восьм|'
                      r'девят|десят)[а-яё]*\b', t)
        if m:
            return _ORDINALS[m.group(1)]
        if re.search(r'\bпоследн\w*\b', t):
            return -1
        return None