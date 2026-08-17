"""Память ассистента: краткосрочная (реплики диалога), долгосрочная
(факты о пользователе) и история действий (для undo).

Единая точка входа — ConversationMemory (core.py); все модули пишут
в одну SQLite-базу (WAL), один потокобезопасный коннект + блокировка.
"""

from jarvis.memory.core import ConversationMemory

__all__ = ['ConversationMemory']