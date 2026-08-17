"""Системные инструменты: громкость, яркость, уведомления, медиа,
блокировка экрана, скриншоты.

Все вызовы — subprocess с АРГУМЕНТАМИ СПИСКОМ и shell=False. Никаких
строк с shell-подстановками. Каждый инструмент вежливо объясняет,
если соответствующая утилита отсутствует или окружение не поддерживает
действие (X11/Wayland, GNOME/прочее).
"""

import datetime
import os
import re
import shutil
import subprocess

from jarvis import config

# ============================== ВСПОМОГАТЕЛЬНОЕ =============================


def _run(args, timeout=config.TOOL_TIMEOUT_SEC):
    """subprocess.run списком аргументов; возвращает (rc, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or '')
    except FileNotFoundError:
        return 127, '', f'не найдена команда: {args[0]}'
    except subprocess.TimeoutExpired:
        return 124, '', 'таймаут'


def _tool_missing(tool, suggestion):
    return f'Утилита {tool} не установлена. {suggestion}'


# ============================== ГРОМКОСТЬ ==================================

# Приоритет: wpctl (PipeWire/WirePlumber) -> pactl (PulseAudio/pipewire-pulse)


def _volume_change(step, direction):
    """direction: '+' или '-'. Возвращает (ok, message)."""
    step = max(1, min(100, int(step or 5)))
    sign = f'{direction}{step}%'

    if shutil.which('wpctl'):
        rc, out, err = _run(['wpctl', 'set-volume',
                             '@DEFAULT_AUDIO_SINK@', sign])
        if rc == 0:
            verb = 'увеличил' if direction == '+' else 'уменьшил'
            return True, f'Громкость {verb} на {step}%.'
    if shutil.which('pactl'):
        rc, out, err = _run(['pactl', 'set-sink-volume',
                             '@DEFAULT_SINK@', sign])
        if rc == 0:
            verb = 'увеличил' if direction == '+' else 'уменьшил'
            return True, f'Громкость {verb} на {step}%.'
    return False, _tool_missing(
        'wpctl/pactl', 'Установите pipewire-pulse или pulseaudio-utils.')


def volume_up(step=None):
    return _volume_change(step, '+')


def volume_down(step=None):
    return _volume_change(step, '-')


# ============================== ЯРКОСТЬ ====================================


def _brightness_change(step, direction):
    step = max(1, min(100, int(step or 5)))
    if not shutil.which('brightnessctl'):
        return False, _tool_missing(
            'brightnessctl', 'Установите её (Arch: brightnessctl).')
    rc, out, err = _run(['brightnessctl', 'set', f'{direction}{step}%'])
    if rc == 0:
        verb = 'увеличил' if direction == '+' else 'уменьшил'
        return True, f'Яркость {verb} на {step}%.'
    # brightnessctl вернул ошибку — часто «нет устройств яркости»
    rc_dev, out_dev, _ = _run(['brightnessctl', '--list'])
    if rc_dev != 0 or not out_dev.strip():
        return False, ('Не нашёл устройств управления яркостью — похоже, '
                       'этот ноутбук/монитор их не поддерживает.')
    return False, f'Не удалось изменить яркость: {err.strip()[:200]}'


def brightness_up(step=None):
    return _brightness_change(step, '+')


def brightness_down(step=None):
    return _brightness_change(step, '-')


# ============================== УВЕДОМЛЕНИЯ =================================


def notify(message, title='Jarvis'):
    """notify-send. В Wayland/GNOME работает через dbus-сервис уведомлений."""
    if not message:
        return False, 'Нечего показывать: пустое сообщение.'
    if not shutil.which('notify-send'):
        return False, _tool_missing(
            'notify-send', 'Установите libnotify (Arch: libnotify).')
    rc, out, err = _run(['notify-send', '-a', 'Jarvis',
                         '-u', 'normal', title, message[:400]])
    if rc == 0:
        return True, f'Показал уведомление: {message[:80]}'
    return False, f'notify-send не сработал: {err.strip()[:200]}'


# ============================== МЕДИАПЛЕЕР =================================


def media_play_pause(action=None):
    """playerctl: play-pause/next/previous/stop/play/pause."""
    action = (action or 'play-pause').lower()
    valid = {'play-pause', 'next', 'previous', 'stop', 'play', 'pause'}
    if action not in valid:
        return False, f'Неизвестное действие для плеера: {action}'
    if not shutil.which('playerctl'):
        return False, _tool_missing(
            'playerctl', 'Установите её (Arch: playerctl).')
    rc, out, err = _run(['playerctl', action])
    if rc == 0:
        names = {'play-pause': 'поставил на паузу/продолжил',
                 'next': 'переключил на следующий трек',
                 'previous': 'переключил на предыдущий трек',
                 'stop': 'остановил воспроизведение',
                 'play': 'запустил воспроизведение',
                 'pause': 'поставил на паузу'}
        return True, names[action] + '.'
    if rc == 1 and 'No players found' in (err + out):
        return False, 'Сейчас не запущен ни один медиаплеер, которым управляет playerctl.'
    return False, f'playerctl не сработал: {err.strip()[:200]}'


# ============================== БЛОКИРОВКА ЭКРАНА ============================


def lock_screen():
    """Блокировка экрана: GNOME (X11 и Wayland) -> loginctl (systemd-logind).

    Ни одна команда не требует root/sudo.
    """
    # 1) GNOME Shell / ScreenSaver через session D-Bus
    if shutil.which('dbus-send'):
        rc, _, err = _run(['dbus-send', '--session',
                           '--type=method_call',
                           '--dest=org.gnome.ScreenSaver',
                           '/org/gnome/ScreenSaver',
                           'org.gnome.ScreenSaver.Lock'])
        if rc == 0:
            return True, 'Заблокировал экран.'
    # 2) systemd-logind: блокируем активную сессию пользователя
    if shutil.which('loginctl'):
        rc, out, err = _run(['loginctl', 'list-sessions',
                             '--no-legend', '--no-pager'])
        if rc == 0:
            session_id = None
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] == os.environ.get('USER', ''):
                    session_id = parts[0]
                    break
            if session_id:
                rc2, _, err2 = _run(['loginctl', 'lock-session', session_id])
                if rc2 == 0:
                    return True, 'Заблокировал экран.'
    return False, ('Не удалось заблокировать экран: не нашёл ни GNOME '
                   'ScreenSaver, ни активной сессии loginctl.')


# ============================== СКРИНШОТ ====================================


def _portal_screenshot(out_path):
    """Скриншот через xdg-desktop-portal (единственный универсальный способ
    на GNOME Wayland). Возвращает путь или None."""
    token = f'jv{int(datetime.datetime.now().timestamp() * 1000) % 100000}'
    opts = ('{{"handle_token": <"{t}">, "modal": <false>, '
            '"interactive": <true>}}').format(t=token)
    subprocess.Popen(['gdbus', 'call', '--session',
                      '--dest', 'org.freedesktop.portal.Desktop',
                      '--object-path', '/org/freedesktop/portal/desktop',
                      '--method',
                      'org.freedesktop.portal.Screenshot.Screenshot',
                      '', opts],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = subprocess.run(['gdbus', 'monitor', '--session',
                            '--dest', 'org.freedesktop.portal.Desktop'],
                           capture_output=True, text=True, timeout=20)
        out = r.stdout or ''
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or '') if isinstance(e.stdout, str) else ''
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if token in line and 'Response' in line:
            for chunk in lines[i:i + 3]:
                m = re.search(r"'uri':\s*<'file://([^']+)'>", chunk)
                if m:
                    return os.path.expanduser(m.group(1))
    return None


def screenshot():
    """Скриншот в ~/Pictures/Screenshots. GNOME-screenshot -> grim (wlroots)
    -> import (X11/ImageMagick) -> xdg-desktop-portal (Wayland)."""
    out_dir = os.path.expanduser('~/Pictures/Screenshots')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(
        out_dir,
        'screenshot_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.png')

    if shutil.which('gnome-screenshot'):
        rc, _, err = _run(['gnome-screenshot', '-f', path])
        if rc == 0:
            return True, f'Скриншот сохранён: {path}'
    elif shutil.which('grim'):   # wlroots compositors (Sway и т.п.)
        rc, _, err = _run(['grim', path])
        if rc == 0:
            return True, f'Скриншот сохранён: {path}'
    elif shutil.which('import'):  # X11
        rc, _, err = _run(['import', '-window', 'root', path])
        if rc == 0:
            return True, f'Скриншот сохранён: {path}'

    # Последний резерв — портал (работает на GNOME Wayland)
    portal_path = _portal_screenshot(path)
    if portal_path:
        return True, f'Скриншот сохранён: {portal_path}'
    return False, ('Не удалось сделать скриншот: нет gnome-screenshot, grim '
                   'или import, а портал не ответил. На GNOME в первый раз '
                   'появится диалог разрешения скриншотов.')


# ============================== РЕЕСТР-ХУК ==================================
# Инструменты регистрируются в registry.py; здесь только реализации.
TOOLS = {
    'volume_up': volume_up,
    'volume_down': volume_down,
    'brightness_up': brightness_up,
    'brightness_down': brightness_down,
    'notify': notify,
    'media_play_pause': media_play_pause,
    'lock_screen': lock_screen,
    'screenshot': screenshot,
}