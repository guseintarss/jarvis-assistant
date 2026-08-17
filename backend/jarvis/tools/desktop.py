"""Открытие приложений через .desktop-файлы (без shell и без root).

Как это работает:
  1. сканируются стандартные каталоги .desktop-файлов
     (/usr/share/applications, ~/.local/share/applications,
      flatpak exports и т.д.);
  2. имя из запроса сопоставляется: точный id -> алиас -> подстрока
     в Name/Keywords -> близкое по difflib;
  3. запуск: gio launch <файл> (правильный способ для GNOME, работает
     и на X11, и на Wayland), запасные варианты: gtk-launch <id>,
     xdg-open <файл>.

Никаких shell-команд: аргументы передаются списком, subprocess с
shell=False. Исполняемая строка из .desktop не парсится вручную —
запуск делает gio, который сам разбирает Exec безопасно.
"""

import difflib
import os
import subprocess
import shutil

# Стандартные каталоги с .desktop-файлами
DESKTOP_DIRS = [
    '/usr/share/applications',
    '/usr/local/share/applications',
    os.path.expanduser('~/.local/share/applications'),
    os.path.expanduser('~/.local/share/flatpak/exports/share/applications'),
    '/var/lib/flatpak/exports/share/applications',
    '/var/lib/snapd/desktop/applications',
]

# Алиасы: русские/обиходные имена -> известные desktop-id.
# Ключ — нормализованное имя (без пробелов, нижний регистр).
ALIASES = {
    'браузер': 'firefox',
    'интернет': 'firefox',
    'терминал': 'gnome-terminal',
    'консоль': 'gnome-terminal',
    'калькулятор': 'gnome-calculator',
    'файловыйменеджер': 'nautilus',
    'файлы': 'nautilus',
    'проводник': 'nautilus',
    'почта': 'thunderbird',
    'почтовыйклиент': 'thunderbird',
    'текстовыйредактор': 'gedit',
    'редактор': 'gedit',
    'блокнот': 'gedit',
    'настройки': 'gnome-control-center',
    'музыка': 'spotify',
    'медиаплеер': 'vlc',
    'видеоплеер': 'vlc',
    'плеер': 'vlc',
    'календарь': 'gnome-calendar',
    'заметки': 'obsidian',
    'картинки': 'gnome-photos',
    'телеграм': 'telegramdesktop',
    'дискорд': 'discord',
    'слак': 'slack',
    'скриншоты': 'gnome-screenshot',
}

_CACHE = None  # {desktop_id: path, name_ru, name_en, keywords, exec_base}


def _parse_desktop(path):
    """Извлекает метаданные .desktop-файла (безопасно, без exec-парсинга
    — запуск делает gio)."""
    entry = {'path': path, 'name_ru': '', 'name_en': '',
             'keywords': '', 'exec_base': ''}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            in_desktop = False
            for line in f:
                line = line.strip()
                if line.startswith('['):
                    in_desktop = line == '[Desktop Entry]'
                    continue
                if not in_desktop or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                value = value.strip()
                if key == 'Name[ru]':
                    entry['name_ru'] = value.lower()
                elif key == 'Name':
                    entry['name_en'] = value.lower()
                elif key == 'Keywords[ru]':
                    entry['keywords'] += ' ' + value.lower()
                elif key == 'Keywords':
                    entry['keywords'] += ' ' + value.lower()
                elif key == 'Exec':
                    # только базовое имя команды для сопоставления
                    # (у части .desktop-файлов Exec пустой — пропускаем)
                    exec_parts = value.split()
                    if exec_parts:
                        entry['exec_base'] = exec_parts[0].lstrip('/').lower()
    except OSError:
        pass
    return entry


def _app_cache():
    """Кэш всех .desktop-файлов: {id: entry}."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cache = {}
    seen = set()
    for directory in DESKTOP_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            for name in sorted(os.listdir(directory)):
                if not name.endswith('.desktop'):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                entry = _parse_desktop(os.path.join(directory, name))
                desktop_id = name[:-len('.desktop')].lower()
                cache[desktop_id] = entry
        except OSError:
            continue
    _CACHE = cache
    return cache


def _normalize(name):
    return ''.join(ch for ch in name.lower() if ch.isalnum())


def find_desktop(name):
    """Ищет .desktop-запись по имени. Возвращает (desktop_id, entry) или
    (None, None)."""
    if not name:
        return None, None
    norm = _normalize(name)
    cache = _app_cache()
    if not cache:
        return None, None

    # 1) точное совпадение desktop-id
    if norm in cache:
        return norm, cache[norm]
    # 2) алиас
    alias = ALIASES.get(norm)
    if alias and alias in cache:
        return alias, cache[alias]
    # 3) подстрока в имени (ru и en), keywords, exec_base
    for desktop_id, entry in cache.items():
        haystack = (entry['name_ru'] + ' ' + entry['name_en'] + ' '
                    + entry['keywords'] + ' ' + entry['exec_base'])
        if norm and norm in _normalize(haystack):
            return desktop_id, entry
    # 4) близкое по Левенштейну (difflib) к русскому/английскому имени
    names = {entry['name_ru'] or entry['name_en']: desktop_id
             for desktop_id, entry in cache.items()
             if entry['name_ru'] or entry['name_en']}
    best = difflib.get_close_matches(name.lower(), names, n=1, cutoff=0.6)
    if best:
        desktop_id = names[best[0]]
        return desktop_id, cache[desktop_id]
    return None, None


def launch_app(name):
    """Открывает приложение. Возвращает (ok, message)."""
    desktop_id, entry = find_desktop(name)
    if not entry:
        return False, f'Не нашёл приложение «{name}» среди установленных.'

    # Основной способ: gio launch (GNOME, X11 и Wayland). Короткий таймаут:
    # если приложение уже запущено, gio может висеть на активации (в
    # голосовом режиме пауза в 20 с выглядит как зависание ассистента).
    gio_timed_out = False
    if shutil.which('gio'):
        try:
            r = subprocess.run(['gio', 'launch', entry['path']],
                               capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            gio_timed_out = True
            r = None
        if r is not None and r.returncode == 0:
            return True, f'Открыл приложение «{name}».'
    # Запасной: gtk-launch по id (тот же D-Bus-механизм — пропускаем,
    # если gio уже повис на активации приложения)
    if shutil.which('gtk-launch') and not gio_timed_out:
        r = subprocess.run(['gtk-launch', desktop_id],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True, f'Открыл приложение «{name}».'
    # Последний запасной: xdg-open на сам .desktop-файл
    if shutil.which('xdg-open'):
        r = subprocess.run(['xdg-open', entry['path']],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True, f'Открыл приложение «{name}».'
    return False, f'Не смог запустить «{name}»: нет gio/gtk-launch/xdg-open.'