"""Внешние сервисы: погода, новости, почта, календарь.

    check_weather   — wttr.in (без ключа), кэш 10 минут в памяти;
    check_news      — RSS-фиды через xml.etree (без новых зависимостей),
                      фиды перебираются по очереди до первого успеха;
    send_email      — SMTP с подтверждением (risk high), настройки ТОЛЬКО
                      из переменных окружения (секретов в коде нет);
    check_calendar  — ближайшие события из локального .ics (datetime+regex).

Сеть — только HTTPS, таймауты жёсткие, ошибки сети превращаются
в вежливые ответы (ассистент не должен «падать»).
"""

import datetime
import os
import re
import smtplib
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText

import requests

from jarvis import config

# ============================== ПОГОДА ======================================

_weather_cache = {'ts': 0.0, 'city': '', 'text': ''}


def check_weather(city=''):
    """Погода из wttr.in (формат: погода + температура + ветер)."""
    city = (city or '').strip() or 'Москва'
    now = time.time()
    if (_weather_cache['city'] == city
            and now - _weather_cache['ts'] < 600):
        return True, _weather_cache['text']
    url = config.WEATHER_API_URL.format(
        city=urllib.parse.quote(city))
    try:
        r = requests.get(url, timeout=config.WEATHER_API_TIMEOUT_SEC)
        r.raise_for_status()
        text = r.text.strip()
        if not text or 'Unknown location' in text:
            return False, f'Не нашёл погоду для города «{city}».'
        _weather_cache.update(ts=time.time(), city=city, text=text)
        return True, f'Погода в {city}: {text}'
    except requests.RequestException:
        return False, 'Погода недоступна: нет сети или сервис не отвечает.'


# ============================== НОВОСТИ =====================================


def check_news():
    """Топ новостей из RSS (первый рабочий фид)."""
    last_error = ''
    for feed in config.NEWS_FEEDS:
        try:
            r = requests.get(feed, timeout=config.WEATHER_API_TIMEOUT_SEC,
                             headers={'User-Agent': 'jarvis-assistant/1.0'})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            items = _parse_feed(root)
            if not items:
                continue
            lines = [f'{i + 1}) {title}' for i, (title, _) in
                     enumerate(items[:config.NEWS_MAX_ITEMS])]
            return True, 'Свежие новости:\n' + '\n'.join(lines)
        except (requests.RequestException, ET.ParseError) as exc:
            last_error = str(exc)
    return False, f'Новости недоступны: {last_error[:150] or "нет сети"}'


def _parse_feed(root):
    """RSS 2.0 (rss/channel/item) или Atom (feed/entry) -> [(title, link)]."""
    items = []
    for item in root.iter():
        if item.tag.endswith('item') or item.tag.endswith('entry'):
            title = link = ''
            for child in item:
                if child.tag.endswith('title'):
                    title = (child.text or '').strip()
                elif child.tag.endswith('link'):
                    if child.text:
                        link = child.text.strip()
                    else:
                        link = child.get('href', '')
            if title:
                items.append((title, link))
    return items


# ============================== ПОЧТА =======================================

# Безопасный экспорт: из config не тащим SMTP_PASSWORD в сообщения об ошибках


def send_email(to='', text=''):
    """Отправляет письмо по SMTP. Все параметры — из окружения
    (JARVIS_SMTP_HOST/PORT/USER/PASSWORD/TO), секретов в коде нет.

    Высокий риск: Executor запросит подтверждение до вызова.
    """
    if not text.strip():
        return False, 'Скажите текст письма (например, «отправь письмо с текстом отчёт готов»).'
    recipient = (to or '').strip() or config.SMTP_DEFAULT_TO
    if not recipient:
        return False, 'Не указан адрес получателя (JARVIS_SMTP_TO не задан).'
    if not (config.SMTP_HOST and config.SMTP_USER):
        return False, ('SMTP не настроен: задайте JARVIS_SMTP_HOST, '
                       'JARVIS_SMTP_USER, JARVIS_SMTP_PASSWORD и JARVIS_SMTP_TO.')
    msg = MIMEText(text[:10000], 'plain', 'utf-8')
    msg['Subject'] = 'Jarvis'
    msg['From'] = config.SMTP_USER
    msg['To'] = recipient
    try:
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                      timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                                  timeout=20)
            server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except (smtplib.SMTPException, OSError) as exc:
        return False, f'Не удалось отправить письмо: {exc}'
    return True, f'Письмо отправлено на {recipient}.'


# ============================== КАЛЕНДАРЬ ===================================

def check_calendar():
    """Ближайшие события из локального .ics (календарь пользователя).

    Путь — config.CALENDAR_ICS_PATH (по умолчанию в каталоге данных,
    переопределяется JARVIS_CALENDAR_ICS). VEVENT'ы разбираются регэкспами
    и datetime — без новых зависимостей.
    """
    path = config.CALENDAR_ICS_PATH
    if not os.path.isfile(path):
        return False, (f'Файл календаря не найден: {path}. Создайте .ics '
                       '(или укажите JARVIS_CALENDAR_ICS).')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as exc:
        return False, f'Не удалось прочитать календарь: {exc}'
    events = []
    now = datetime.datetime.now().astimezone()
    for block in re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', content,
                            re.DOTALL):
        summary = _ics_field(block, 'SUMMARY')
        dtstart = _ics_field(block, 'DTSTART')
        if not summary or not dtstart:
            continue
        when = _parse_ics_time(dtstart)
        if when is None or when < now - datetime.timedelta(days=1):
            continue
        events.append((when, summary))
    events.sort()
    if not events:
        return True, 'Ближайших событий в календаре нет.'
    lines = [f'{when.strftime("%d.%m %H:%M")} — {summary}'
             for when, summary in events[:8]]
    return True, 'Ближайшие события:\n' + '\n'.join(lines)


def _ics_field(block, name):
    for line in block.splitlines():
        if line.startswith(name + ':'):
            return line[len(name) + 1:].strip()
    return ''


def _parse_ics_time(value):
    """'20260817T183000' (local) или с суффиксом Z -> aware datetime или None."""
    value = value.strip()
    if len(value) >= 15:
        try:
            dt = datetime.datetime.strptime(value[:15], '%Y%m%dT%H%M%S')
        except ValueError:
            return None
        return dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo) \
            if not value.endswith('Z') else dt.replace(
                tzinfo=datetime.timezone.utc)
    return None


TOOLS = {
    'check_weather': check_weather,
    'check_news': check_news,
    'send_email': send_email,
    'check_calendar': check_calendar,
}