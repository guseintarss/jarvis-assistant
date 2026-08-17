"""Файловые инструменты: открытие файлов/URL и перемещение в корзину.

Все пути перед использованием проходят PathGuard (security.py).
URL открываются только по схемам http/https — file:, javascript: и
прочие схемы отклоняются на уровне валидации.

Удаление — только «в корзину» (gio trash, спецификация freedesktop),
безвозвратного удаления в системе нет вообще.
"""

import os
import shutil
import subprocess
import urllib.parse

from jarvis import config


def _run(args, timeout=config.TOOL_TIMEOUT_SEC):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or '')
    except FileNotFoundError:
        return 127, '', f'не найдена команда: {args[0]}'
    except subprocess.TimeoutExpired:
        return 124, '', 'таймаут'


# ============================== ОТКРЫТИЕ ФАЙЛА ==============================


def open_file(path):
    """Открывает файл через xdg-open. Вызывается ТОЛЬКО после PathGuard."""
    if not path:
        return False, 'Не указан файл.'
    if not shutil.which('xdg-open'):
        return False, 'Утилита xdg-open не установлена.'
    rc, out, err = _run(['xdg-open', path])
    if rc == 0:
        return True, f'Открываю файл: {path}'
    if rc == 3:  # xdg-open: действие не поддерживается для типа файла
        return False, (f'xdg-open не знает, чем открыть «{path}» — '
                       'возможно, нет приложения для этого типа файла.')
    return False, f'Не удалось открыть файл: {err.strip()[:200]}'


# ============================== ОТКРЫТИЕ URL ================================


def validate_url(url):
    """Проверяет URL: только http/https. Возвращает (ok, reason, cleaned)."""
    url = (url or '').strip()
    if not url:
        return False, 'Пустой URL', ''
    if ' ' in url:
        return False, 'URL не должен содержать пробелы', ''
    # без схемы — добавляем https://
    if '://' not in url:
        url = 'https://' + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False, f'Схема {parsed.scheme!r} запрещена (только http/https)', ''
    if not parsed.netloc:
        return False, 'URL без домена', ''
    return True, '', url


def open_url(url):
    """Открывает http/https URL через xdg-open."""
    ok, reason, cleaned = validate_url(url)
    if not ok:
        return False, reason
    if not shutil.which('xdg-open'):
        return False, 'Утилита xdg-open не установлена.'
    rc, out, err = _run(['xdg-open', cleaned])
    if rc == 0:
        return True, f'Открываю в браузере: {cleaned}'
    return False, f'Не удалось открыть URL: {err.strip()[:200]}'


# ============================== КОРЗИНА =====================================


def move_to_trash(paths):
    """Перемещает файлы в корзину через gio trash (спецификация freedesktop,
    восстановимо). Вызывается ТОЛЬКО после PathGuard."""
    if not paths:
        return False, 'Не указан файл.'
    if isinstance(paths, str):
        paths = [paths]
    if not shutil.which('gio'):
        return False, ('Утилита gio (glib2) не установлена — без неё '
                       'безопасного перемещения в корзину нет.')
    moved, failed = [], []
    for p in paths:
        if not os.path.exists(p):
            failed.append(f'{p}: файл не найден')
            continue
        rc, out, err = _run(['gio', 'trash', p])
        if rc == 0:
            moved.append(p)
        else:
            failed.append(f'{p}: {err.strip()[:120]}')
    message = f'Переместил в корзину: {", ".join(os.path.basename(p) for p in moved)}.' \
        if moved else ''
    if failed:
        message += ' Не удалось: ' + '; '.join(failed)
    return bool(moved), message or 'Ничего не перемещено.'