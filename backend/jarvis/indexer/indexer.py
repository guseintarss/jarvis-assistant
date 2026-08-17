"""Индексатор файлов: обход разрешённых папок -> SQLite FTS5.

Что индексируется:
    • только корни из policy.index_roots (не затрагиваются ~/.ssh и пр.);
    • текстовые файлы (policy.index_extensions) — полностью (до лимита
      размера), остальные файлы — по имени и пути;
    • скрытые каталоги и каталоги из denylist пропускаются на корню.

База: ~/.local/share/jarvis-assistant/index.db
    files_fts(path UNINDEXED, name, content, tokenize='unicode61')
    unicode61 корректно токенизирует и кириллицу, и латиницу.

Поиск: MATCH с кавычками каждого слова (безопасная санитизация запроса),
AND между словами — то, что нужно для «найди текст про архитектуру».
"""

import os
import sqlite3

from jarvis import logger


class FileIndexer:
    """Индекс файлов и текста в SQLite FTS5."""

    def __init__(self, db_path, policy, log=None):
        self.db_path = db_path
        self.policy = policy
        self.log = log or logger.get_logger()

    # --------------------------- подключение -------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    # --------------------------- сборка ------------------------------------

    def build(self):
        """Полная пересборка индекса. Возвращает число файлов."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute('DROP TABLE IF EXISTS files_fts')
            conn.execute(
                "CREATE VIRTUAL TABLE files_fts USING fts5("
                "  path UNINDEXED, name, content,"
                "  tokenize='unicode61')")
            count = 0
            for root in self.policy.index_roots:
                count += self._walk(conn, root)
            conn.commit()
            self.log.event('index_built', files=count,
                           roots=len(self.policy.index_roots))
            return count
        finally:
            conn.close()

    def _walk(self, conn, root):
        """Обходит один корень, вставляет файлы. Возвращает число файлов."""
        if not os.path.isdir(root):
            self.log.event('index_skip', root=root, reason='no such dir')
            return 0
        count = 0
        exts = self.policy.index_extensions
        max_bytes = self.policy.index_max_bytes
        for dirpath, dirnames, filenames in os.walk(root):
            # пропускаем скрытые каталоги и запрещённые (denylist)
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.')
                           and not self.policy.is_denied(
                               os.path.join(dirpath, d))]
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if self.policy.is_denied(path):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                content = ''
                if os.path.splitext(filename)[1].lower() in exts \
                        and size <= max_bytes:
                    try:
                        with open(path, encoding='utf-8',
                                  errors='replace') as f:
                            content = f.read(max_bytes)
                    except OSError:
                        content = ''
                conn.execute(
                    'INSERT INTO files_fts(path, name, content) VALUES (?,?,?)',
                    (path, filename, content))
                count += 1
        return count

    # --------------------------- поиск -------------------------------------

    def search(self, query, limit=20):
        """Поиск по индексу. Возвращает список {path, name, snippet}."""
        tokens = self._sanitize(query)
        if not tokens:
            return []
        match_expr = ' AND '.join(f'"{t}"' for t in tokens)
        conn = self._connect()
        try:
            rows = conn.execute(
                'SELECT path, name, snippet(files_fts, 2, "[", "]", "…", 24) '
                'FROM files_fts WHERE files_fts MATCH ? LIMIT ?',
                (match_expr, limit)).fetchall()
            return [{'path': r[0], 'name': r[1], 'snippet': r[2] or ''}
                    for r in rows]
        except sqlite3.OperationalError:
            return []  # «no such table» до первой сборки / синтаксис MATCH
        finally:
            conn.close()

    @staticmethod
    def _sanitize(query):
        """Разбивает запрос на слова, убирает спецсимволы FTS5."""
        words = []
        for token in (query or '').lower().split():
            clean = ''.join(ch for ch in token if ch.isalnum())
            if clean:
                words.append(clean)
        return words[:8]  # ограничение — защита от тяжёлых запросов