"""Анализ ошибок и улучшение ответов при неудаче (ЧАСТЬ 4).

При неудачном выполнении инструмента pipeline вызывает enhance():
сообщение остаётся, но к нему добавляются подсказки:

    • «файлы не найдены»      -> похожие имена из FTS-индекса;
    • «приложение не найдено» -> похожие приложения из .desktop-кэша;
    • «не понял»              -> примеры команд;
    • облако недоступно/ошибка -> совет переформулировать проще + примеры.

Подсказки только дополняют ответ, никогда не изменяют факт неудачи.
"""

from jarvis.recovery import suggestions

# Интенты, ошибки которых усиливаем подсказками файлов/приложений
_FILE_INTENTS = frozenset({'search_files', 'search_text', 'open_file'})
_APP_INTENTS = frozenset({'open_app', 'launch_app'})


def _fmt_list(items, limit=3):
    return ', '.join(f'«{i}»' for i in items[:limit])


def analyze(intent_name, message, query=None, db_path=None):
    """Анализ неудачи -> список подсказок (строк).

    intent_name — интент, который не сработал;
    message     — сообщение инструмента (ok=False);
    query       — текст запроса/слот, по которому ищем похожие файлы;
    db_path     — путь к FTS-индексу (для тестов; по умолчанию конфиг).
    """
    message = message or ''
    hints = []
    lower = message.lower()

    if intent_name in _FILE_INTENTS and 'не найд' in lower:
        names = suggestions.suggest_files(query or '', db_path=db_path)
        if names:
            hints.append(f'Похожие файлы: {_fmt_list(names)}')
        else:
            hints.append('Попробуйте переформулировать запрос или '
                         'уточнить имя файла.')

    elif intent_name in _APP_INTENTS and ('не нашёл' in lower
                                          or 'не найдено' in lower):
        apps = suggestions.suggest_apps(query or '')
        if apps:
            hints.append(f'Похожие приложения: {_fmt_list(apps)}')
        else:
            hints.append('Попробуйте назвать приложение точнее, '
                         'например «открой браузер».')

    elif 'не понял' in lower or 'непонятн' in lower:
        hints.append('Попробуйте, например: '
                     + _fmt_list(suggestions.suggest_commands()))

    return hints


def enhance(intent_name, message, query=None, db_path=None):
    """message + подсказки. Возвращает (message, hints)."""
    hints = analyze(intent_name, message, query=query, db_path=db_path)
    if not hints:
        return message, []
    return message + '\n\n' + '\n'.join(hints), hints


def cloud_fallback_hint():
    """Подсказка при недоступности облачной модели."""
    return ('Облачная модель сейчас недоступна, но я могу выполнить '
            'простые команды локально — например, '
            + _fmt_list(suggestions.suggest_commands()))