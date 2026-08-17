"""Инструменты времени: таймеры, будильники, напоминания, время и дата.

    set_timer        — systemd-run --on-active + notify-send (переживает
                       перезапуск демона);
    set_alarm        — systemd-run --on-calendar в нужное время;
    set_reminder     — запись в SQLite (reminders.db); в заданный момент
                       уведомление показывает планировщик (proactive/);
    list_reminders   — активные напоминания из БД;
    cancel_reminder  — отмена по id или всех;
    check_time       — локальное время;
    check_date       — дата и день недели.

Все «простое» — детерминированный код, без нейросетей. systemd-run
запускается списком аргументов (shell=False), никаких строк с командами.
"""

import datetime
import os
import re
import subprocess

from jarvis import config

# ============================== ВСПОМОГАТЕЛЬНОЕ =============================


def _run(args, timeout=config.TOOL_TIMEOUT_SEC):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or '')
    except FileNotFoundError:
        return 127, '', f'не найдена команда: {args[0]}'
    except subprocess.TimeoutExpired:
        return 124, '', 'таймаут'


def _parse_duration(text):
    """'2 часа 30 минут' -> секунды (int) или None при неверном формате."""
    total = 0
    for m in re.finditer(r'(\d+)\s*(час\w*|минут\w*|секунд\w*)', text):
        num = int(m.group(1))
        unit = m.group(2)
        if unit.startswith('час'):
            total += num * 3600
        elif unit.startswith('минут'):
            total += num * 60
        else:
            total += num
    if total <= 0:
        # «полчаса» / «полминуты» — словами
        if 'полчаса' in text or 'пол часа' in text:
            return 1800
        if 'полминуты' in text or 'пол минуты' in text:
            return 30
        return None
    return total


def _parse_time_hhmm(text):
    """'7:30' / '7 30' -> (часы, минуты); иначе None."""
    m = re.search(r'(\d{1,2})\s*[:\.]\s*(\d{2})', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _parse_day_offset(text):
    """'сегодня/завтра/послезавтра/в понедельник...' -> offset дней."""
    t = text.lower()
    if 'послезавтра' in t:
        return 2
    if 'завтра' in t:
        return 1
    if 'сегодня' in t:
        return 0
    weekday = {'понедельник': 0, 'вторник': 1, 'среду': 2, 'среду ': 2,
               'четверг': 3, 'пятницу': 4, 'пятниц': 4, 'субботу': 5,
               'воскресенье': 6}
    for word, idx in weekday.items():
        if word in t:
            today = datetime.date.today().weekday()
            return (idx - today) % 7
    return 0


# ============================== ТАЙМЕР И БУДИЛЬНИК ==========================


def set_timer(duration):
    """Устанавливает таймер: через N секунд — уведомление notify-send."""
    seconds = _parse_duration(duration or '')
    if seconds is None:
        return False, ('Не понял длительность таймера. Скажите, например, '
                       '«поставь таймер на 5 минут».')
    if not _systemd_available():
        return False, ('Не найден systemd-run — без него таймер поставить '
                       'нельзя (нужен systemd user session).')
    rc, _, err = _run(['systemd-run', '--user', '--on-active', f'{seconds}s',
                       'notify-send', '-a', 'Jarvis', 'Таймер',
                       f'Время вышло (таймер {_fmt_duration(seconds)})!'])
    if rc == 0:
        return True, f'Таймер установлен на {_fmt_duration(seconds)}.'
    return False, f'systemd-run не сработал: {err.strip()[:200]}'


def set_alarm(time=None, hour=None):
    """Будильник: systemd-run --on-calendar в указанное время HH:MM."""
    hh, mm = _resolve_time(time, hour)
    if hh is None:
        return False, ('Не понял время будильника. Скажите, например, '
                       '«будильник на 7:30».')
    when = datetime.datetime.now().replace(hour=hh, minute=mm, second=0)
    if when <= datetime.datetime.now():
        when += datetime.timedelta(days=1)
    calendar = when.strftime('%Y-%m-%d %H:%M:%S')
    if not _systemd_available():
        return False, ('Не найден systemd-run — без него будильник поставить '
                       'нельзя (нужен systemd user session).')
    rc, _, err = _run(['systemd-run', '--user', '--on-calendar', calendar,
                       'notify-send', '-a', 'Jarvis', 'Будильник',
                       f'Будильник на {hh:02d}:{mm:02d}!'])
    if rc == 0:
        return True, f'Будильник установлен на {hh:02d}:{mm:02d}.'
    return False, f'systemd-run не сработал: {err.strip()[:200]}'


def _resolve_time(time, hour):
    """Собирает (часы, минуты) из слотов time (HH:MM) и hour ('7 утра')."""
    if time:
        parsed = _parse_time_hhmm(time)
        if parsed:
            hh, mm = parsed
            if hh < 24 and mm < 60:
                return hh, mm
    if hour:
        m = re.search(r'(\d{1,2})', hour)
        if m and 'вечера' in hour.lower() and int(m.group(1)) < 12:
            return int(m.group(1)) + 12, 0
        if m and int(m.group(1)) < 24:
            return int(m.group(1)), 0
    return None, None


def _systemd_available():
    try:
        r = subprocess.run(['systemd-run', '--user', '--no-ask-password',
                            '--on-active', '1s', 'true'],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 or 'job' in (r.stdout or '').lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _fmt_duration(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f'{h} ч')
    if m:
        parts.append(f'{m} мин')
    if s or not parts:
        parts.append(f'{s} с')
    return ' '.join(parts)


# ============================== НАПОМИНАНИЯ =================================

from jarvis.proactive.reminders import ReminderStore  # noqa: E402 — единый
# источник правды: сюда пишут инструменты, отсюда читает планировщик.


def set_reminder(time=None, day=None, text=''):
    """Напоминание: 'завтра в 18:00 полить цветы' -> запись в БД."""
    if not text.strip():
        return False, ('Не понял, о чём напомнить. Скажите, например, '
                       '«напомни завтра в 18:00 полить цветы».')
    offset = _parse_day_offset(day or '')
    target = datetime.date.today() + datetime.timedelta(days=offset)
    parsed = _parse_time_hhmm(time or '')
    if parsed:
        hh, mm = parsed
    else:
        hh, mm = 9, 0  # время не сказали — напомнить утром
    when = datetime.datetime.combine(target, datetime.time(hh, mm))
    if when <= datetime.datetime.now():
        when += datetime.timedelta(days=1)
    store = ReminderStore()
    try:
        rid = store.add(when.isoformat(), text)
        return True, (f'Напомню «{text}» {when.strftime("%d.%m %H:%M")} '
                      f'(№{rid}).')
    finally:
        store.close()


def list_reminders():
    """Список активных напоминаний с id и временем."""
    store = ReminderStore()
    try:
        rows = store.upcoming()
        if not rows:
            return True, 'Активных напоминаний нет.'
        lines = [f'№{rid}: {when[:16].replace("T", " ")} — {text}'
                 for rid, when, text in rows]
        return True, 'Напоминания:\n' + '\n'.join(lines)
    finally:
        store.close()


def cancel_reminder(target=None):
    """Отменяет напоминание по id, 'последнее' или все ('все'/'всё')."""
    target = (target or '').lower().strip()
    store = ReminderStore()
    try:
        rows = store.upcoming()
        if not rows:
            return True, 'Активных напоминаний нет.'
        if target in ('все', 'всё'):
            store.clear()
            return True, f'Отменил все {len(rows)} напоминаний.'
        if 'последн' in target:
            rid = rows[-1][0]
            store.delete(rid)
            return True, f'Отменил напоминание №{rid}.'
        if target.isdigit():
            rid = int(target)
            if any(r[0] == rid for r in rows):
                store.delete(rid)
                return True, f'Отменил напоминание №{rid}.'
            return False, f'Напоминания №{rid} нет в списке активных.'
        return False, 'Скажите номер напоминания, «последнее» или «все».'
    finally:
        store.close()


# ============================== ВРЕМЯ И ДАТА =================================


def check_time():
    now = datetime.datetime.now()
    return True, f'Сейчас {now.strftime("%H:%M")}.'


def check_date():
    now = datetime.datetime.now()
    weekday = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница',
               'суббота', 'воскресенье')[now.weekday()]
    return True, (f'Сегодня {now.strftime("%d.%m.%Y")} — {weekday}.')


# ============================== РЕЕСТР-ХУК ===================================

TOOLS = {
    'set_timer': set_timer,
    'set_alarm': set_alarm,
    'set_reminder': set_reminder,
    'check_time': check_time,
    'check_date': check_date,
    'list_reminders': list_reminders,
    'cancel_reminder': cancel_reminder,
}