"""Буфер обмена: копирование, чтение и история (своя, в SQLite).

    clipboard_copy    — копирование текста (или выделения) в clipboard
                        через xclip (аргументы списком, текст — через stdin);
    clipboard_paste   — показать содержимое буфера обмена;
    clipboard_history — история копирований (своя таблица, xclip историю
                        не хранит). Лимит 50 записей.

Никакого shell: subprocess.run(['xclip', ...]) + input=.
"""

import os
import sqlite3
import subprocess

from jarvis import config

_HISTORY_LIMIT = 50


def _xclip_available():
    import shutil
    return shutil.which('xclip') is not None


def _history_conn():
    os.makedirs(os.path.dirname(config.CLIPBOARD_HISTORY_DB_PATH),
                exist_ok=True)
    conn = sqlite3.connect(config.CLIPBOARD_HISTORY_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS clipboard (
                        id   INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts   TEXT NOT NULL,
                        text TEXT NOT NULL)''')
    conn.commit()
    return conn


def clipboard_copy(text=''):
    """Копирует текст в буфер обмена; если текста нет — текущее выделение."""
    if not text.strip():
        if not _xclip_available():
            return False, 'xclip не установлен (установите: sudo pacman -S xclip).'
        # копируем текущее выделение (primary -> clipboard)
        try:
            r = subprocess.run(['xclip', '-selection', 'primary', '-o'],
                               capture_output=True, text=True, timeout=5)
            text = r.stdout or ''
        except (OSError, subprocess.TimeoutExpired):
            text = ''
        if not text.strip():
            return False, 'Нет выделенного текста и текст не указан.'
    if not _xclip_available():
        return False, 'xclip не установлен (установите: sudo pacman -S xclip).'
    try:
        r = subprocess.run(['xclip', '-selection', 'clipboard', '-i'],
                           input=text, capture_output=True, text=True,
                           timeout=5)
        if r.returncode != 0:
            return False, f'xclip не сработал: {(r.stderr or "").strip()[:200]}'
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f'xclip не сработал: {exc}'
    # история (своя)
    try:
        conn = _history_conn()
        conn.execute('INSERT INTO clipboard (ts, text) VALUES (?, ?)',
                     (datetime_now(), text[:4000]))
        conn.execute('DELETE FROM clipboard WHERE id NOT IN '
                     '(SELECT id FROM clipboard ORDER BY id DESC LIMIT ?)',
                     (_HISTORY_LIMIT,))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    return True, f'Скопировал в буфер обмена: {text[:80]}'


def clipboard_paste():
    """Возвращает содержимое буфера обмена (xclip -o)."""
    if not _xclip_available():
        return False, 'xclip не установлен (установите: sudo pacman -S xclip).'
    try:
        r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False, 'Не удалось прочитать буфер обмена.'
    content = (r.stdout or '').strip()
    if not content:
        return True, 'Буфер обмена пуст.'
    return True, f'В буфере обмена: {content[:500]}'


def clipboard_history():
    """Последние копирования (своя история, до 50 записей)."""
    try:
        conn = _history_conn()
        rows = conn.execute(
            'SELECT id, ts, text FROM clipboard ORDER BY id DESC LIMIT 10'
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return False, 'История буфера обмена недоступна.'
    if not rows:
        return True, 'История пуста — пока ничего не копировалось.'
    lines = []
    for rid, ts, text in reversed(rows):
        preview = text[:60].replace('\n', ' ')
        lines.append(f'{rid}) {preview}{"…" if len(text) > 60 else ""}')
    return True, 'История буфера обмена:\n' + '\n'.join(lines)


def datetime_now():
    import datetime
    return datetime.datetime.now().astimezone().isoformat()


TOOLS = {
    'clipboard_copy': clipboard_copy,
    'clipboard_paste': clipboard_paste,
    'clipboard_history': clipboard_history,
}