"""Долгосрочная память: факты о пользователе (SQLite, таблица facts).

Факты извлекаются из реплик детерминированными регэкспами БЕЗ нейросети:
    «меня зовут Алексей»            -> name: Алексей
    «мне 25 лет»                    -> age: 25
    «мой email: a@b.ru»             -> email: a@b.ru
    «я работаю в Яндексе»           -> job: Яндексе
    «я живу в Москве»               -> city: Москве
    «запомни, что я люблю кофе»     -> fact: я люблю кофе

Ключи фактов стабильны (upsert): новая реплика с тем же фактом
перезаписывает значение, а не плодит дубликаты.
"""

import re

from jarvis import logger

# (pattern, ключ факта); ключ None — «запомни, что ...» -> общий факт
_FACT_PATTERNS = (
    (r'(?:меня зовут|моё имя|меня называ[юе]т)\s+'
     r'([А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-z\-]+){0,2})', 'name'),
    (r'мне\s+(\d{1,3})\s+(?:год|года|лет)\b', 'age'),
    (r'(?:мой|моя|моё)\s+email\s*(?:—|:|=|-)?\s*'
     r'([\w.+\-]+@[\w\-]+\.[\w.]+)', 'email'),
    (r'я\s+работаю\s+(?:в|на)\s+([^\s,.;!?]{2,40})', 'job'),
    (r'(?:я\s+живу\s+в|мой\s+город\s*—?\s*|мой город)'
     r'\s*([А-ЯЁA-Z][а-яёa-z\-]+)', 'city'),
    (r'я\s+учусь\s+в\s+([^\s,.;!?]{2,40})', 'school'),
)

# «запомни, что ...» / «запомни: ...» — общий факт из произвольной фразы
_GENERIC_RE = re.compile(
    r'(?:запомни|запомните|запомнить)\s*(?:,\s*)?(?:что\s*)?'
    r'(?:это\s*)?(.+)', re.IGNORECASE)


def _fact_key(phrase):
    """Стабильный ключ для общего факта: первые слова латиницей/кириллицей."""
    slug = re.sub(r'[^а-яёa-z0-9]+', '_', phrase.lower()).strip('_')
    return 'fact:' + slug[:60]


class LongTermMemory:
    """Факты о пользователе с извлечением из обычных реплик."""

    def __init__(self, conn, lock, log=None):
        self._conn = conn
        self._lock = lock
        self.log = log or logger.get_logger()

    # --------------------------- извлечение --------------------------------

    def remember(self, text):
        """Извлекает факты из реплики и сохраняет их.

        Возвращает список (key, value) сохранённых фактов (может быть пустым).
        """
        text = (text or '').strip()
        if not text:
            return []
        found = []
        for pattern, key in _FACT_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                found.append((key, m.group(1).strip()))
        m = _GENERIC_RE.search(text)
        if m and m.group(1).strip():
            found.append((_fact_key(m.group(1)), m.group(1).strip()))
        for key, value in found:
            self.set(key, value, source=text)
        return found

    # --------------------------- запись/чтение ------------------------------

    def set(self, key, value, source=''):
        """Сохраняет/обновляет факт (upsert по ключу)."""
        if not key or not value:
            return
        with self._lock:
            self._conn.execute(
                'INSERT INTO facts (key, value, source, ts) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, '
                'source=excluded.source, ts=excluded.ts',
                (key, value[:500], (source or '')[:1000], logger.datetime_now()))
            self._conn.commit()

    def get(self, key):
        """Значение факта по ключу (или None)."""
        cur = self._conn.execute('SELECT value FROM facts WHERE key=?', (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def facts(self):
        """Все факты как dict {key: value}."""
        cur = self._conn.execute('SELECT key, value FROM facts ORDER BY ts')
        return dict(cur.fetchall())

    def forget(self, key):
        """Удаляет факт (для тестов и «забудь, что ...»)."""
        with self._lock:
            self._conn.execute('DELETE FROM facts WHERE key=?', (key,))
            self._conn.commit()