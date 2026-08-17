"""Поиск файлов (fd) и текста (ripgrep).

Безопасность:
    • аргументы передаются списком, shell=False — ни одна строка из
      пользовательского запроса не исполняется;
    • поиск идёт ТОЛЬКО внутри разрешённых корней (policy.allowed_roots),
      а запрещённые каталоги (denylist: ~/.ssh, ~/.config и т.д.)
      исключаются glob-паттернами — ripgrep/fd их даже не открывают;
    • количество результатов ограничено (SEARCH_MAX_RESULTS).
"""

import os
import shutil
import subprocess

from jarvis import config
from jarvis import logger


def _run(args, timeout=config.TOOL_TIMEOUT_SEC):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or '')
    except FileNotFoundError:
        return 127, '', f'не найдена команда: {args[0]}'
    except subprocess.TimeoutExpired:
        return 124, '', 'таймаут'


def _deny_globs(root, denied_paths):
    """Превращает denylist-каталоги внутри root в glob-паттерны для fd/rg."""
    globs = []
    for denied in denied_paths:
        if denied == root or denied.startswith(root + os.sep):
            rel = os.path.relpath(denied, root)
            globs.append(f'!{rel}/**')
            globs.append(f'!{rel}')
    return globs


def _default_root(policy):
    """Корень поиска по умолчанию: первый allowed_root внутри home."""
    for root in policy.allowed_roots:
        if root.startswith(os.path.expanduser('~')):
            return root
    return policy.allowed_roots[0] if policy.allowed_roots else None


# ============================== ФАЙЛЫ ======================================


def search_files(policy, query, root=None, limit=None):
    """Поиск файлов по имени (fd). Возвращает (ok, message, results)."""
    if not query:
        return False, 'Не указано, что искать.', []
    if not shutil.which('fd'):
        return False, 'Утилита fd не установлена (попробуйте: sudo pacman -S fd).', []
    limit = limit or config.SEARCH_MAX_RESULTS
    root = root or _default_root(policy)
    if not root or not os.path.isdir(root):
        return False, f'Каталог поиска не существует: {root}', []

    args = ['fd', '-t', 'f', '-i', '--max-results', str(limit),
            '--no-ignore', query, root]
    args.extend(_deny_globs(root, policy.denied_paths))

    rc, out, err = _run(args)
    if rc not in (0, 1):  # 1 = «ничего не найдено» у fd
        return False, f'fd не сработал: {err.strip()[:200]}', []
    results = [line.strip() for line in out.splitlines() if line.strip()]
    if not results:
        return False, f'По запросу «{query}» файлы не найдены.', []
    message = (f'Нашёл {len(results)} файл(ов) по запросу «{query}»'
               + (f' (показываю первые {limit})' if len(results) >= limit else '')
               + ':')
    return True, message, results[:limit]


# ============================== ТЕКСТ ======================================


def search_text(policy, query, root=None, limit=None):
    """Поиск текста в файлах (rg). Возвращает (ok, message, results)."""
    if not query:
        return False, 'Не указано, какой текст искать.', []
    if not shutil.which('rg'):
        return False, 'Утилита ripgrep не установлена (попробуйте: sudo pacman -S ripgrep).', []
    limit = limit or config.SEARCH_MAX_RESULTS
    root = root or _default_root(policy)
    if not root or not os.path.isdir(root):
        return False, f'Каталог поиска не существует: {root}', []

    args = ['rg', '-l', '-i', '--no-heading', '--max-count', '5',
            '--max-filesize', config.SEARCH_TEXT_MAX_FILESIZE,
            '--no-ignore', query, root]
    # у rg glob-паттерны задаются ТОЛЬКО флагом -g: позиционным аргументом
    # паттерн будет принят за путь и rg упадёт с «No such file or directory»
    for glob in _deny_globs(root, policy.denied_paths):
        args.extend(['-g', glob])

    rc, out, err = _run(args)
    if rc not in (0, 1):  # 1 = «совпадений нет» у rg
        return False, f'rg не сработал: {err.strip()[:200]}', []
    results = [line.strip() for line in out.splitlines() if line.strip()]
    if not results:
        return False, f'Текст «{query}» нигде не найден.', []
    message = (f'Нашёл «{query}» в {len(results)} файл(ах)'
               + (f' (показываю первые {limit})' if len(results) >= limit else '')
               + ':')
    return True, message, results[:limit]