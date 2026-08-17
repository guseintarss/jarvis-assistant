"""ConversationMemory — единая точка памяти ассистента.

Объединяет:
    short_term    — последние 20 реплик (контекст диалога);
    long_term     — факты о пользователе (SQLite);
    actions       — история действий (для undo).

Потокобезопасность: один sqlite3-коннект (check_same_thread=False) +
общая блокировка. Если БД недоступна — память отключается (enabled=False)
и ассистент продолжает работать без контекста, ни одно исключение не
должно «утечь» в обработку запроса.

Интеграция (pipeline.Assistant.process):
    1. до обработки запроса — контекст уже загружен (см. context());
    2. после ответа — add_exchange() сохраняет реплики и извлекает факты;
    3. при «открой второй файл» — resolve_reference() подставляет
       нужный элемент из последнего ответа в слоты.
"""

import os
import sqlite3
import threading

from jarvis import logger
from jarvis.memory import action_history
from jarvis.memory import long_term
from jarvis.memory import short_term

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    ts     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    tool    TEXT NOT NULL,
    params  TEXT NOT NULL DEFAULT '{}',
    ok      INTEGER NOT NULL DEFAULT 1,
    message TEXT NOT NULL DEFAULT ''
);
"""


class ConversationMemory:
    """Единая точка доступа к памяти ассистента."""

    def __init__(self, db_path, log=None):
        self.log = log or logger.get_logger()
        self.enabled = True
        self.db_path = db_path
        self._conn = None
        self._lock = threading.Lock()
        self.short_term = None
        self.long_term = None
        self.actions = None
        try:
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA busy_timeout=3000')
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self.short_term = short_term.ShortTermMemory(self._conn, self._lock,
                                                         log=self.log)
            self.long_term = long_term.LongTermMemory(self._conn, self._lock,
                                                      log=self.log)
            self.actions = action_history.ActionHistory(self._conn, self._lock,
                                                        log=self.log)
        except Exception as exc:  # noqa: BLE001 — память не должна ронять демон
            self.enabled = False
            self.log.event('memory_disabled', error=str(exc))

    # --------------------------- диалог ------------------------------------

    def add_exchange(self, user_text, assistant_text, intent=None):
        """Сохраняет реплику пользователя, ответ ассистента и извлекает факты."""
        if not self.enabled:
            return
        self.short_term.add('user', user_text)
        if assistant_text:
            self.short_term.add('assistant', assistant_text)
        facts = self.long_term.remember(user_text)
        if facts:
            self.log.event('facts_extracted', facts=dict(facts),
                           intent=intent)

    def context(self):
        """Контекст для обработки: реплики, факты, последние действия.

        Возвращает dict с 'turns', 'facts', 'last_actions' и готовой
        строкой 'text' для системного промпта облачной LLM.
        """
        if not self.enabled:
            return {'turns': [], 'facts': {}, 'last_actions': [],
                    'text': ''}
        turns = self.short_term.turns()
        facts = self.long_term.facts()
        last_actions = self.actions.last(5)
        lines = []
        for role, text in turns[-8:]:
            who = 'Пользователь' if role == 'user' else 'Ассистент'
            lines.append(f'{who}: {text}')
        for key, value in facts.items():
            lines.append(f'Факт: {key} = {value}')
        return {
            'turns': turns,
            'facts': facts,
            'last_actions': last_actions,
            'text': '\n'.join(lines),
        }

    # --------------------------- ссылки ------------------------------------

    def resolve_reference(self, user_text, intent_name):
        """«Открой второй» -> {'path': 'акт.pdf'}; {} если ссылки нет.

        Работает для интентов со слотом из списка последнего ответа
        (open_file/open_app). Без нейросети: порядковое слово из реплики
        + пронумерованные пункты из последнего ответа ассистента.
        """
        if not self.enabled:
            return {}
        index = short_term.ShortTermMemory.ordinal_index(user_text)
        if index is None:
            return {}
        item = self.short_term.resolve_ordinal(index)
        if item is None:
            return {}
        slot = {'open_file': 'path', 'open_app': 'app'}.get(intent_name)
        if not slot:
            return {}
        return {slot: item}

    # --------------------------- undo --------------------------------------

    def undo_last(self):
        """Последнее успешное действие (или None), готовое к откату."""
        if not self.enabled:
            return None
        return self.actions.undo_last()

    # --------------------------- жизненный цикл ----------------------------

    def close(self):
        """Закрывает соединение (при выгрузке демона)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None