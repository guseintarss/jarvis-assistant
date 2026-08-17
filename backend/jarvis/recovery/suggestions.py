"""Подсказки: похожие файлы, приложения и примеры команд (ЧАСТЬ 4).

Все подсказки — детерминированные, без нейросетей:
    • похожие файлы       — FTS5-индекс (prefix-поиск) + difflib;
    • похожие приложения  — кэш .desktop-записей (difflib);
    • примеры команд      — примеры фраз из обучающего датасета.

Подсказки вызываются ТОЛЬКО при неудачном выполнении инструмента,
поэтому не влияют на производительность успешных запросов.
"""

import difflib
import os
import sqlite3

from jarvis import config

# Сколько подсказок показывать максимум
SUGGESTIONS_LIMIT = 3

# Примеры команд для подсказки «Команда непонятна» (статические, быстрые)
_COMMAND_EXAMPLES = (
    '«открой браузер»',
    '«найди файл договор»',
    '«поставь таймер на 5 минут»',
    '«сколько будет 25 умножить на 37»',
    '«какая погода в Москве»',
)


# ============================== ФАЙЛЫ =======================================


def _fts_names(query, db_path=None, limit=20):
    """Имена файлов из FTS-индекса по префиксам слов запроса.

    Возвращает список (path, name). None — индекс недоступен.
    """
    tokens = []
    for token in (query or '').lower().split():
        clean = ''.join(ch for ch in token if ch.isalnum())
        if clean:
            tokens.append(clean)
    if not tokens:
        return None
    match_expr = ' AND '.join(f'{t}*' for t in tokens[:4])
    try:
        conn = sqlite3.connect(db_path or config.INDEX_DB_PATH)
        try:
            rows = conn.execute(
                'SELECT path, name FROM files_fts '
                'WHERE files_fts MATCH ? LIMIT ?',
                (match_expr, limit)).fetchall()
            return [(r[0], r[1]) for r in rows]
        except sqlite3.OperationalError:
            return None  # индекса нет или MATCH-синтаксис не подошёл
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def _fts_sample(db_path=None, limit=300):
    """Выборка имён из индекса (для fuzzy-поиска, когда префиксы не нашли)."""
    try:
        conn = sqlite3.connect(db_path or config.INDEX_DB_PATH)
        try:
            rows = conn.execute(
                'SELECT path, name FROM files_fts LIMIT ?',
                (limit,)).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def suggest_files(query, db_path=None, limit=SUGGESTIONS_LIMIT):
    """Похожие файлы: префиксы в FTS + difflib к именам.

    Возвращает список имён файлов (базовые имена); [] если индекс недоступен
    или совпадений нет.
    """
    if not query:
        return []
    from_prefix = True
    pairs = _fts_names(query, db_path)
    if not pairs:
        # префиксы не нашли ничего — для fuzzy берём выборку из индекса
        pairs = _fts_sample(db_path)
        from_prefix = False
    if not pairs:
        return []
    names = list(dict.fromkeys(p[1] for p in pairs))
    close = difflib.get_close_matches(query.lower(), names,
                                      n=limit, cutoff=0.4)
    if close:
        return close
    # без близости показываем только то, что реально нашёл поиск по префиксу
    # (выборка индекса без совпадений — не подсказка)
    return names[:limit] if from_prefix else []


# ============================== ПРИЛОЖЕНИЯ ==================================


def suggest_apps(query, limit=SUGGESTIONS_LIMIT):
    """Похожие приложения из кэша .desktop-записей (difflib).

    Возвращает список названий; [] если кэш недоступен.
    """
    if not query:
        return []
    try:
        from jarvis.tools.desktop import _app_cache
        cache = _app_cache()
    except Exception:  # noqa: BLE001 — подсказка не должна ломать ответ
        return []
    if not cache:
        return []
    names = []
    for entry in cache.values():
        for field in (entry.get('name_ru'), entry.get('name_en')):
            if field and field not in names:
                names.append(field)
    return difflib.get_close_matches(query.lower(), names,
                                     n=limit, cutoff=0.5)


# ============================== КОМАНДЫ =====================================


def suggest_commands(limit=3):
    """Примеры команд для подсказки «не понял»."""
    return list(_COMMAND_EXAMPLES[:limit])