"""Тесты памяти ассистента (jarvis.memory).

Проверяем: краткосрочную память (лимит 20, персистентность), факты
(извлечение из реплик, upsert), историю действий (push/undo), единую
точку ConversationMemory и резолвинг «открой второй файл».

Запуск (из каталога backend/):
    python -m unittest tests.test_memory -v
"""

import os
import tempfile
import unittest

from jarvis.memory.core import ConversationMemory
from jarvis.memory.short_term import ShortTermMemory


class MemoryTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'memory.db')

    def tearDown(self):
        self.tmp.cleanup()


class TestShortTerm(MemoryTestCase):

    def test_limit_20(self):
        mem = ConversationMemory(self.db_path)
        for i in range(30):
            mem.short_term.add('user', f'реплика {i}')
        turns = mem.short_term.turns()
        self.assertEqual(len(turns), 20)
        self.assertEqual(turns[0][1], 'реплика 10')
        self.assertEqual(turns[-1][1], 'реплика 29')

    def test_persistence_across_reopen(self):
        mem = ConversationMemory(self.db_path)
        mem.short_term.add('user', 'привет')
        mem.short_term.add('assistant', 'здравствуйте')
        mem.close()
        mem2 = ConversationMemory(self.db_path)
        self.assertEqual(mem2.short_term.turns(),
                         [('user', 'привет'), ('assistant', 'здравствуйте')])

    def test_last_assistant(self):
        mem = ConversationMemory(self.db_path)
        mem.short_term.add('user', 'найди файлы')
        mem.short_term.add('assistant', 'Нашёл 3 файла:\n1) один.txt\n2) два.txt')
        self.assertEqual(mem.short_term.last_assistant(),
                         'Нашёл 3 файла:\n1) один.txt\n2) два.txt')

    def test_ordinal_index(self):
        self.assertEqual(ShortTermMemory.ordinal_index('открой второй'), 1)
        self.assertEqual(ShortTermMemory.ordinal_index('покажи первый файл'), 0)
        self.assertEqual(ShortTermMemory.ordinal_index('открой последний'), -1)
        self.assertIsNone(ShortTermMemory.ordinal_index('открой браузер'))


class TestLongTerm(MemoryTestCase):

    def test_extract_name(self):
        mem = ConversationMemory(self.db_path)
        facts = mem.long_term.remember('Меня зовут Алексей')
        self.assertIn(('name', 'Алексей'), facts)
        self.assertEqual(mem.long_term.get('name'), 'Алексей')

    def test_extract_email(self):
        mem = ConversationMemory(self.db_path)
        facts = mem.long_term.remember('мой email: alex@example.com')
        self.assertIn(('email', 'alex@example.com'), facts)

    def test_generic_fact(self):
        mem = ConversationMemory(self.db_path)
        facts = mem.long_term.remember('Запомни, что я люблю кофе')
        self.assertTrue(any(k.startswith('fact:') for k, _ in facts))
        stored = mem.long_term.facts()
        self.assertIn('я люблю кофе', stored.values())

    def test_upsert_not_duplicate(self):
        mem = ConversationMemory(self.db_path)
        mem.long_term.remember('Меня зовут Алексей')
        mem.long_term.remember('Меня зовут Алексей Петров')
        self.assertEqual(len(mem.long_term.facts()), 1)
        self.assertEqual(mem.long_term.get('name'), 'Алексей Петров')

    def test_no_facts_from_random_phrase(self):
        mem = ConversationMemory(self.db_path)
        self.assertEqual(mem.long_term.remember('Открой браузер'), [])


class TestActionHistory(MemoryTestCase):

    def test_push_and_last(self):
        mem = ConversationMemory(self.db_path)
        mem.actions.push('notify', {'message': 'привет'}, ok=True)
        mem.actions.push('move_to_trash', {'paths': ['a.txt']}, ok=False,
                         message='нет файла')
        last = mem.actions.last()
        self.assertEqual(len(last), 2)
        self.assertEqual(last[0]['tool'], 'notify')
        self.assertTrue(last[0]['ok'])
        self.assertEqual(last[1]['tool'], 'move_to_trash')
        self.assertFalse(last[1]['ok'])
        self.assertEqual(last[1]['params'], {'paths': ['a.txt']})

    def test_undo_returns_last_successful(self):
        mem = ConversationMemory(self.db_path)
        mem.actions.push('notify', {}, ok=True)
        mem.actions.push('open_url', {'url': 'https://x.ru'}, ok=True)
        mem.actions.push('volume_up', {}, ok=False)
        action = mem.actions.undo_last()
        self.assertEqual(action['tool'], 'open_url')
        remaining = mem.actions.last()
        self.assertEqual([a['tool'] for a in remaining], ['notify', 'volume_up'])

    def test_undo_empty(self):
        mem = ConversationMemory(self.db_path)
        self.assertIsNone(mem.actions.undo_last())


class TestCore(MemoryTestCase):

    def test_add_exchange_and_context(self):
        mem = ConversationMemory(self.db_path)
        mem.add_exchange('Меня зовут Алексей', 'Приятно познакомиться!')
        mem.add_exchange('открой браузер', 'Открываю браузер.')
        ctx = mem.context()
        self.assertEqual(len(ctx['turns']), 4)
        self.assertEqual(ctx['facts'], {'name': 'Алексей'})
        self.assertIn('Факт: name = Алексей', ctx['text'])
        self.assertIn('Пользователь: открой браузер', ctx['text'])

    def test_resolve_second_file(self):
        mem = ConversationMemory(self.db_path)
        mem.add_exchange('Найди договор аренды', 'Нашёл 3 файла:\n'
                         '1) договор_аренда.pdf\n2) договор_купли.pdf\n'
                         '3) акт.pdf')
        slots = mem.resolve_reference('Открой второй', 'open_file')
        self.assertEqual(slots, {'path': 'договор_купли.pdf'})

    def test_resolve_last_app(self):
        mem = ConversationMemory(self.db_path)
        mem.add_exchange('Найди приложения', 'Нашёл:\n1) Firefox\n2) VS Code')
        slots = mem.resolve_reference('открой последний', 'open_app')
        self.assertEqual(slots, {'app': 'VS Code'})

    def test_resolve_no_reference(self):
        mem = ConversationMemory(self.db_path)
        self.assertEqual(mem.resolve_reference('открой браузер', 'open_app'), {})

    def test_disabled_db(self):
        # родительский «каталог» — обычный файл: БД открыть нельзя
        blocker = os.path.join(self.tmp.name, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as f:
            f.write('x')
        mem = ConversationMemory(os.path.join(blocker, 'memory.db'))
        self.assertFalse(mem.enabled)
        mem.add_exchange('привет', 'здравствуйте')
        self.assertEqual(mem.context()['turns'], [])
        self.assertIsNone(mem.undo_last())


if __name__ == '__main__':
    unittest.main()