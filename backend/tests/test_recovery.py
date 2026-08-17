"""Тесты ЧАСТИ 4: обработка ошибок и подсказки.

Подсказки файлов проверяются на настоящем FTS5-индексе (временная БД),
приложения — на подменённом кэше .desktop, end-to-end — через pipeline
с минимальной политикой (поиск по временному каталогу).
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from jarvis import policy as policy_mod
from jarvis.recovery import error_handler
from jarvis.recovery import suggestions


def _build_fts_index(db_path, names):
    """Создаёт FTS5-индекс с той же схемой, что и FileIndexer."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE VIRTUAL TABLE files_fts USING fts5("
        "  path UNINDEXED, name, content, tokenize='unicode61')")
    for name in names:
        conn.execute('INSERT INTO files_fts (path, name, content) VALUES (?, ?, ?)',
                     (f'/home/u/{name}', name, ''))
    conn.commit()
    conn.close()


class TestSuggestFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'index.db')

    def test_prefix_matches(self):
        _build_fts_index(self.db, ['договор_аренда.pdf', 'договор_купли.pdf',
                                   'отчёт.xlsx'])
        names = suggestions.suggest_files('договор', db_path=self.db)
        self.assertIn('договор_аренда.pdf', names)

    def test_fuzzy_fallback(self):
        # «аренды» не начинается с «аренда*» — FTS не находит, но difflib
        # по выборке из индекса подбирает близкое имя
        _build_fts_index(self.db, ['договор-аренда.pdf', 'музыка.mp3'])
        names = suggestions.suggest_files('договор аренды', db_path=self.db)
        self.assertIn('договор-аренда.pdf', names)

    def test_no_match_returns_empty(self):
        _build_fts_index(self.db, ['музыка.mp3'])
        self.assertEqual(suggestions.suggest_files('отчёт', db_path=self.db), [])

    def test_missing_index_returns_empty(self):
        self.assertEqual(
            suggestions.suggest_files('x', db_path=os.path.join(self.tmp, 'нет.db')),
            [])

    def test_empty_query(self):
        self.assertEqual(suggestions.suggest_files('', db_path=self.db), [])


class TestSuggestApps(unittest.TestCase):
    def test_close_matches(self):
        cache = {
            'firefox.desktop': {'name_ru': 'Firefox', 'name_en': 'Firefox'},
            'org.gnome.Nautilus.desktop': {'name_ru': 'Файлы',
                                           'name_en': 'Files'},
            'code.desktop': {'name_ru': 'VS Code', 'name_en': 'Code'},
        }
        with mock.patch('jarvis.tools.desktop._app_cache', return_value=cache):
            apps = suggestions.suggest_apps('файл')
        self.assertEqual(apps, ['Файлы'])

    def test_no_cache(self):
        with mock.patch('jarvis.tools.desktop._app_cache', return_value={}):
            self.assertEqual(suggestions.suggest_apps('x'), [])


class TestErrorHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_file_not_found_hint(self):
        db = os.path.join(self.tmp, 'idx.db')
        _build_fts_index(db, ['договор_аренда.pdf', 'музыка.mp3'])
        message, hints = error_handler.enhance(
            'search_files', 'По запросу «договор» файлы не найдены.',
            query='договор', db_path=db)
        self.assertTrue(hints)
        self.assertIn('Похожие файлы', hints[0])

    def test_app_not_found_hint(self):
        cache = {'org.gnome.Nautilus.desktop': {'name_ru': 'Файлы',
                                                'name_en': 'Files'}}
        with mock.patch('jarvis.tools.desktop._app_cache', return_value=cache):
            message, hints = error_handler.enhance(
                'open_app', 'Не нашёл приложение «фалы» среди установленных.',
                query='фалы')
        self.assertIn('Похожие приложения', hints[0])

    def test_unclear_command_hint(self):
        _, hints = error_handler.enhance('set_timer',
                                         'Не понял длительность таймера.')
        self.assertTrue(hints)
        self.assertIn('Попробуйте', hints[0])

    def test_unrelated_message_no_hints(self):
        message, hints = error_handler.enhance('open_app', 'Открыл приложение.')
        self.assertEqual(hints, [])
        self.assertEqual(message, 'Открыл приложение.')

    def test_cloud_fallback_hint(self):
        hint = error_handler.cloud_fallback_hint()
        self.assertIn('локально', hint)
        self.assertIn('«поставь таймер на 5 минут»', hint)


class TestPipelineIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_search_failure_gets_suggestion(self):
        """End-to-end: неудачный поиск -> подсказка в ответе."""
        tmp = tempfile.mkdtemp()
        open(os.path.join(tmp, 'договор_аренда.pdf'), 'w').close()
        open(os.path.join(tmp, 'музыка.mp3'), 'w').close()
        db = os.path.join(self.tmp, 'idx.db')
        _build_fts_index(db, ['договор_аренда.pdf', 'музыка.mp3'])

        policy = policy_mod.Policy({'allowed_roots': [tmp],
                                    'index_roots': [tmp]},
                                   source='test-recovery')
        with mock.patch('jarvis.config.INDEX_DB_PATH', db):
            from jarvis.pipeline import make_assistant
            assistant = make_assistant(policy, auto_yes=True)
            result = assistant.process('найди файл договорк')
        self.assertEqual(result['route'], 'local')
        self.assertFalse(result['ok'])
        self.assertIn('Похожие файлы', result['response'])


if __name__ == '__main__':
    unittest.main()