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
# ============================== СИСТЕМНАЯ ИНФОРМАЦИЯ =========================
# Всё — только стандартная библиотека (без psutil): /proc, os, shutil, socket.


def _read_proc(path, default=''):
    """Читает /proc-файл с запасом на отсутствие/ошибки чтения."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return default


def system_info():
    """ОС, ядро, CPU, RAM, аптайм и нагрузка (из /proc, без psutil)."""
    import platform
    uname = platform.uname()
    cpu = os.cpu_count() or 0
    meminfo = {}
    for line in _read_proc('/proc/meminfo').splitlines():
        key, _, val = line.partition(':')
        meminfo[key.strip()] = val.strip()
    def _mb(key):
        try:
            return int(meminfo.get(key, '0').split()[0]) // 1024
        except (ValueError, IndexError):
            return 0
    mem_total, mem_avail = _mb('MemTotal'), _mb('MemAvailable')
    uptime_s = float(_read_proc('/proc/uptime', '0').split()[0] or 0)
    uptime = f'{int(uptime_s // 3600)} ч {int(uptime_s % 3600 // 60)} мин'
    load = _read_proc('/proc/loadavg').split()[:3]
    return True, (
        f'Система: {uname.system} {uname.release} ({uname.machine})\n'
        f'Хост: {uname.node}\n'
        f'CPU: {cpu} ядер\n'
        f'Память: занято {mem_total - mem_avail} МБ из {mem_total} МБ\n'
        f'Аптайм: {uptime}\n'
        f'Нагрузка: {" ".join(load)}')


def check_disk():
    """Свободное место на корневом разделе."""
    usage = shutil.disk_usage('/')
    free_gb = usage.free / 1024 ** 3
    total_gb = usage.total / 1024 ** 3
    pct = usage.used / usage.total * 100 if usage.total else 0
    return True, (f'На диске свободно {free_gb:.1f} ГБ из {total_gb:.1f} ГБ '
                  f'(занято {pct:.0f}%).')


def check_battery():
    """Заряд батареи из /sys/class/power_supply (BAT*)."""
    import glob
    batteries = sorted(glob.glob('/sys/class/power_supply/BAT*'))
    if not batteries:
        return False, ('Батарея не найдена — похоже, это настольный '
                       'компьютер без АКБ.')
    lines = []
    for bat in batteries:
        capacity = _read_proc(os.path.join(bat, 'capacity'), '').strip()
        status = _read_proc(os.path.join(bat, 'status'), '').strip()
        name = os.path.basename(bat)
        if capacity:
            lines.append(f'{name}: {capacity}% '
                         f'({status or "неизвестно"})')
    if not lines:
        return False, 'Заряд батареи не читается (нет файла capacity).'
    return True, 'Батарея:\n' + '\n'.join(lines)


def check_network():
    """Проверка сети: есть ли доступ в интернет + локальные интерфейсы."""
    import socket
    reachable = False
    for host, port in (('1.1.1.1', 53), ('8.8.8.8', 53)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            reachable = True
            break
        except OSError:
            continue
    try:
        ifaces = [name for name, _ in socket.if_nameindex()]
    except OSError:
        ifaces = []
    if reachable:
        return True, ('Интернет есть. Сетевые интерфейсы: '
                      + ', '.join(ifaces) if ifaces else '')
    return False, ('Сети нет: не удалось достучаться ни до одного хоста. '
                   'Интерфейсы: ' + ', '.join(ifaces) if ifaces else 'Сети нет.')


def list_processes(n=None):
    """Топ процессов по памяти (/proc/*/stat + status)."""
    try:
        top = int(n or 10)
        top = max(1, min(50, top))
    except (TypeError, ValueError):
        top = 10
    procs = []
    for pid_dir in os.listdir('/proc'):
        if not pid_dir.isdigit():
            continue
        pid = pid_dir
        stat = _read_proc(f'/proc/{pid}/stat', '')
        rss_kb = 0
        if stat:
            try:
                parts = stat.rsplit(')', 1)
                rss_kb = int(parts[1].split()[21]) * 4096 // 1024
            except (IndexError, ValueError):
                rss_kb = 0
        comm = _read_proc(f'/proc/{pid}/comm', '?').strip()[:30]
        procs.append((rss_kb, pid, comm))
    procs.sort(reverse=True)
    lines = [f'{pid}: {comm} ({rss} МБ)'
             for rss, pid, comm in procs[:top]]
    return True, 'Топ процессов по памяти:\n' + '\n'.join(lines)


def kill_process(pid=None):
    """Завершает процесс по PID (SIGTERM). Высокий риск — подтверждение
    запрашивает Executor (policy). Свой собственный PID — не трогаем."""
    try:
        target = int(pid or '')
    except (TypeError, ValueError):
        return False, 'Скажите PID процесса числом (например, «убей процесс 1234»).'
    if target <= 1 or target > 4194304:
        return False, f'PID {target} вне допустимого диапазона.'
    if target == os.getpid():
        return False, 'Я не могу убить сам себя.'
    try:
        os.kill(target, 15)  # SIGTERM — вежливый вариант завершения
    except ProcessLookupError:
        return False, f'Процесс {target} не существует.'
    except PermissionError:
        return False, f'Нет прав на завершение процесса {target}.'
    return True, f'Отправил сигнал завершения процессу {target}.'


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
    'system_info': system_info,
    'check_disk': check_disk,
    'check_battery': check_battery,
    'check_network': check_network,
    'list_processes': list_processes,
    'kill_process': kill_process,
}