#!/usr/bin/env python3
"""
Ева — голосовой ассистент (фоновый демон).

Пайплайн:
  1. Постоянно слушает микрофон маленькой Vosk-моделью и ищет слово-активатор
     ("ева" и похожие варианты распознавания).
  2. После активации записывает команду (пока пользователь не замолчит).
  3. Распознаёт команду через faster-whisper (точнее, чем маленький Vosk).
  4. Отправляет текст в облачную LLM (OpenAI-совместимый API) и получает ответ.
  5. Озвучивает ответ женским голосом (RHVoice — если установлен, иначе
     Piper "irina") и проигрывает через paplay/aplay.
  6. Публикует состояние и ответы в D-Bus (org.jarvis.Assistant), которые
     слушает GNOME Shell расширение.

Перед запуском отредактируйте секцию CONFIG ниже под свою систему.
"""

import collections
import asyncio
import base64
import datetime
import difflib
import io
import json
import logging
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
import socket
import xml.etree.ElementTree as ET

import numpy as np
import re
import requests
import sounddevice as sd
from vosk import Model as VoskModel, KaldiRecognizer

import gi
gi.require_version('GLib', '2.0')
gi.require_version('Gio', '2.0')
from gi.repository import GLib, Gio

# ============================== CONFIG ===================================
# Все настройки вынесены в config.py (тот же каталог) — редактируйте их там.
# Приватные значения (API-ключи, секреты) можно задавать переменными
# окружения или файлом ~/.config/jarvis-assistant/config — см. config.py.

from config import *  # noqa: F401,F403 — константы CONFIG-секции
from config import _http  # noqa: F401 — общая HTTP-сессия с keep-alive

_dbus_connection = None
_main_loop = None


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    stream=sys.stderr,
)


def _log(msg):
    logging.info(msg)


def _warn(msg):
    logging.warning(msg)


def _err(msg):
    logging.error(msg)


def init_dbus():
    """Регистрирует объект org.jarvis.Assistant на шине сессии."""
    global _dbus_connection

    _dbus_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    node_info = Gio.DBusNodeInfo.new_for_xml(DBUS_INTROSPECTION)
    iface_info = node_info.lookup_interface(DBUS_INTERFACE_NAME)

    _dbus_connection.register_object_with_closures2(
        DBUS_OBJECT_PATH,
        iface_info,
        _on_method_call,
        _on_get_property,
        None,
    )

    def _on_name_lost(_conn, _name, error):
        if error is not None:
            _log(f'[dbus] не удалось занять имя {DBUS_BUS_NAME}: {error.message} '
                 f'(другой экземпляр демона уже запущен?)')
            if _main_loop is not None:
                GLib.idle_add(_main_loop.quit)

    Gio.bus_own_name_on_connection(
        _dbus_connection,
        DBUS_BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        None,
        _on_name_lost,
    )
    _log('[jarvis] D-Bus сервис опубликован')


def _emit_signal(name, variant):
    """Шлёт сигнал на шину (вызывается только из main-потока через idle_add)."""
    if _dbus_connection is None:
        return
    try:
        _dbus_connection.emit_signal(
            None, DBUS_OBJECT_PATH, DBUS_INTERFACE_NAME, name, variant,
        )
    except GLib.Error as e:
        _log(f'[dbus] не удалось отправить сигнал {name}: {e}')


def _on_method_call(_conn, _sender, _path, _iface, method_name, params, invocation):
    method = getattr(service, method_name, None)
    if method is None:
        invocation.return_dbus_error(
            'org.jarvis.Assistant.Error', f'Неизвестный метод {method_name}')
        return
    try:
        if method_name == 'SetActivationMode':
            mode = params.unpack()[0] if params is not None else ''
            method(mode)
        else:
            method()
        invocation.return_value(None)
    except Exception as e:
        _log(f'[dbus] ошибка при вызове {method_name}: {e}')
        invocation.return_dbus_error('org.jarvis.Assistant.Error', str(e))


def _on_get_property(_conn, _sender, _path, _iface, property_name):
    value = getattr(service, property_name, None)
    if value is None:
        return None
    return GLib.Variant('s', value)


class JarvisService:
    VALID_MODES = ('voice', 'hotkey', 'both')

    def __init__(self):
        self._state = 'offline'
        self._last_response = ''
        self.paused = False
        self.activation_mode = ACTIVATION_MODE
        self.manual_activation_event = threading.Event()
        self.stop_event = threading.Event()

    # --- свойства, которые видит расширение ---
    @property
    def State(self):
        return self._state

    @property
    def LastResponse(self):
        return self._last_response

    # --- вызываются из фонового потока через GLib.idle_add ---
    def set_state(self, state):
        self._state = state
        _emit_signal('StateChanged', GLib.Variant('(s)', (state,)))
        return False  # для GLib.idle_add — выполнить один раз

    def set_response(self, text):
        self._last_response = text
        _emit_signal('ResponseReady', GLib.Variant('(s)', (text,)))
        return False

    def set_heard(self, text):
        """Распознанная команда — расширение показывает её на «острове»."""
        _emit_signal('Heard', GLib.Variant('(s)', (text,)))
        return False

    # --- методы, вызываемые из расширения (GNOME Shell) ---
    def Activate(self):
        self.manual_activation_event.set()

    def Interrupt(self):
        """Прерывает текущую озвучку/обработку — как повторное слово «Ева»,
        но в отличие от Stop() не останавливает сам демон."""
        interrupt_event.set()
        _kill_player()

    def Stop(self):
        self.stop_event.set()
        if _main_loop is not None:
            GLib.idle_add(_main_loop.quit)

    def TogglePause(self):
        self.paused = not self.paused
        self.set_state('paused' if self.paused else 'idle')

    def SetActivationMode(self, mode):
        if mode not in self.VALID_MODES:
            raise ValueError(f'Неверный режим активации: {mode} '
                             f'(допустимо: {", ".join(self.VALID_MODES)})')
        if mode != self.activation_mode:
            self.activation_mode = mode
            _log(f'[jarvis] режим активации: {mode}')


service = JarvisService()


def emit_state(state):
    GLib.idle_add(service.set_state, state)


def emit_response(text):
    GLib.idle_add(service.set_response, text)


def emit_heard(text):
    GLib.idle_add(service.set_heard, text)


# ============================== AUDIO HELPERS ==============================

audio_queue = queue.Queue()

# Кольцевой буфер последних AUDIO_TAIL_SECONDS аудио: команда, начатая в том
# же вдохе, что и «Ева», не потеряется при активации.
audio_tail = collections.deque(
    maxlen=max(1, int(AUDIO_TAIL_SECONDS * SAMPLE_RATE / BLOCK_SIZE))
)


_mic_gain_smoothed = 1.0


def _boost_chunk(pcm):
    """Нормализует громкость блока: шёпот (тихие блоки) усиливает до уровня
    обычной речи, громкие не трогает. Усиление меняется плавно между блоками,
    чтобы не было щелчков и «насоса» на границе тихо/громко."""
    global _mic_gain_smoothed
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    rms_val = float(np.sqrt(np.mean(samples ** 2)))
    if rms_val > 0:
        target = min(MIC_GAIN_MAX, MIC_GAIN_TARGET_RMS / rms_val)
    else:
        target = MIC_GAIN_MAX
    _mic_gain_smoothed += (target - _mic_gain_smoothed) * 0.4
    samples *= _mic_gain_smoothed
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype(np.int16).tobytes()


def audio_callback(indata, frames, time_info, status):
    if status:
        _err(f'[audio] {status}')
    chunk = _boost_chunk(bytes(indata))
    audio_tail.append(chunk)
    audio_queue.put(chunk)


def rms(pcm_bytes):
    data = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(data) == 0:
        return 0
    return float(np.sqrt(np.mean(data.astype(np.int32) ** 2)))


# Адаптивный порог тишины: шумовой фон подстраивается под микрофон, поэтому
# тихий голос на слабом встроенном микрофоне не теряется. Множитель 1.55 и
# абсолютный минимум 60 подобраны под тихие встроенные микрофоны: раньше
# (2.2 / 150) обычная негромкая речь не дотягивала до порога, и приходилось
# говорить громче. Ложных срабатываний от фонового шума по-прежнему нет —
# шум редко прыгает выше 1.55 фона.
_noise_floor = None
_speech_scale = 1.55


def is_silence(rms_value):
    """True, если rms_value — это шум, а не речь. Порог = шумовой фон * 2.2,
    но не ниже абсолютного минимума 150."""
    global _noise_floor
    if _noise_floor is None:
        _noise_floor = rms_value
    elif rms_value < _noise_floor:
        _noise_floor = rms_value                      # фон упал — мгновенно
    elif rms_value < _noise_floor * 1.5:
        _noise_floor += (rms_value - _noise_floor) * 0.05  # медленно подстраиваемся
    threshold = max(_noise_floor * _speech_scale, 150)
    return rms_value < threshold


def save_wav(path, pcm_bytes, sample_rate=SAMPLE_RATE):
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


# ============================== SYSTEM ACTIONS ==============================
#
# Набор функций, которые LLM может вызывать (function calling / tools через
# Ollama /api/chat и облачный API). Каждая функция принимает dict аргументов и возвращает
# короткую строку-результат, которая идёт обратно в модель как "tool" ответ.
#
# Список сознательно НЕ включает выключение/перезагрузку компьютера и удаление
# файлов — такие необратимые/рискованные действия лучше делать руками. Если
# нужно — добавьте по аналогии, но взвесьте риски случайной активации из-за
# ошибки распознавания речи.

def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, **kwargs)


def action_open_app(args):
    name = (args.get('name') or '').strip().lower()
    if not name:
        return 'Не указано название приложения.'

    # Обычные слова → реальные названия приложений. Модель часто передаёт
    # «браузер»/«терминал» как есть, а таких .desktop-файлов не существует.
    # Для явных алиасов not should_show() не фильтруем (OnlyShowIn и т.п.
    # скрывает приложения в чужой сессии, но запуск всё равно желателен).
    for alias, candidates in APP_ALIASES.items():
        if name != alias and not name.startswith(alias + ' '):
            continue
        for cand in candidates:
            if not cand.endswith('.desktop'):
                cand += '.desktop'
            for app in Gio.AppInfo.get_all():
                if (app.get_id() or '').lower() == cand:
                    app.launch([], None)
                    return f'Открываю {app.get_display_name()}.'
        break

    best = None
    for app in Gio.AppInfo.get_all():
        if not app.should_show():
            continue
        display = (app.get_display_name() or '').lower()
        app_id = (app.get_id() or '').lower()
        if name in display or name in app_id:
            best = app
            if display.startswith(name):
                break  # точное совпадение по началу названия — лучший вариант

    if best is None:
        return f'Не нашёл приложение "{name}" на этом компьютере.'

    try:
        best.launch([], None)
        return f'Открываю {best.get_display_name()}.'
    except GLib.Error as e:
        return f'Не удалось запустить {best.get_display_name()}: {e}'


APP_ALIASES = {
    'браузер': ['firefox', 'firefox.desktop', 'chromium.desktop',
                'google-chrome.desktop', 'org.mozilla.firefox.desktop',
                'org.gnome.Epiphany.desktop', 'microsoft-edge.desktop'],
    'терминал': ['org.gnome.Console.desktop', 'org.gnome.Terminal.desktop',
                 'gnome-terminal.desktop', 'kitty.desktop', 'konsole.desktop',
                 'alacritty.desktop', 'org.wezfurlong.wezterm.desktop'],
    'калькулятор': ['org.gnome.Calculator.desktop', 'gnome-calculator.desktop'],
    'файлы': ['org.gnome.Nautilus.desktop', 'nautilus.desktop', 'io.elementary.files.desktop'],
    'настройки': ['org.gnome.Settings.desktop', 'gnome-control-center.desktop'],
    'текстовый редактор': ['org.gnome.TextEditor.desktop', 'code.desktop',
                           'codium.desktop', 'gedit.desktop'],
    'telegram': ['org.telegram.desktop', 'telegramdesktop.desktop',
                 'telegram.desktop'],
    'slack': ['slack.desktop', 'com.slack.Slack.desktop'],
    'discord': ['discord.desktop', 'com.discordapp.Discord.desktop',
                'org.vesktop.Vesktop.desktop'],
}


# --- «Начать работу»: VS Code + Zen-браузер, всё остальное закрыть ---

WORK_APPS = [
    # процессы (для опознания окон), .desktop-файлы (как запустить),
    # подстроки WM_CLASS (какие окна считать «рабочими» и не закрывать),
    # поисковое имя для open_app
    {
        'name': 'VS Code',
        'processes': ['code-oss', 'code', 'codium'],
        'desktops': ['code-oss.desktop', 'code.desktop', 'codium.desktop'],
        'wm_classes': ['code', 'codium'],
        'search': 'code',
    },
    {
        'name': 'Zen',
        'processes': ['zen-bin', 'zen'],
        'desktops': ['zen.desktop', 'io.github.zen_browser.zen.desktop',
                     'app.zen_browser.zen.desktop'],
        'wm_classes': ['zen'],
        'search': 'zen',
    },
]

# На каких рабочих столах открывать окна при «начни работу»
# (номера 1-based, как в GNOME). Поменяйте под свои привычки.
WORK_APP_WORKSPACES = {
    'VS Code': 1,
    'Zen': 2,
}


def _launch_desktop_app(desktops, search_name):
    """Запускает приложение по .desktop-файлу; если не нашёл — по названию."""
    for desktop in desktops:
        for app in Gio.AppInfo.get_all():
            if (app.get_id() or '').lower() == desktop:
                app.launch([], None)
                return True
    action_open_app({'name': search_name})
    return True


def _close_other_windows(keep_classes):
    """Закрывает все окна рабочего стола (wmctrl), кроме окон приложений
    из keep_classes (сравнение по WM_CLASS из wmctrl -lx)."""
    if not shutil.which('wmctrl'):
        _warn('[jarvis] wmctrl не установлен — не могу закрывать окна')
        return 0
    r = _run(['wmctrl', '-lx'])
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    closed = 0
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        wid, wm_class = parts[0], parts[2]
        if any(k.lower() in wm_class.lower() for k in keep_classes):
            continue
        if _run(['wmctrl', '-ic', wid]).returncode == 0:
            closed += 1
    return closed


def _app_windows(wm_classes):
    """Список (wid, wm_class) окон приложения (по WM_CLASS из wmctrl -lx)."""
    r = _run(['wmctrl', '-lx'])
    if r.returncode != 0:
        return []
    windows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        wid, wm_class = parts[0], parts[2]
        if any(k.lower() in wm_class.lower() for k in wm_classes):
            windows.append((wid, wm_class))
    return windows


def _wait_for_windows(wm_classes, timeout=15):
    """Ждёт появления окна приложения (после запуска оно создаётся
    не мгновенно). Возвращает список (wid, wm_class)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = _app_windows(wm_classes)
        if windows:
            return windows
        time.sleep(0.3)
    return _app_windows(wm_classes)


def _move_window_to_workspace(wid, ws_index):
    """Переносит окно на рабочий стол с индексом ws_index (0-based)."""
    r = _run(['wmctrl', '-ir', wid, '-t', str(ws_index)])
    return r.returncode == 0


_EXTENSION_BUS_NAME = 'org.jarvis.Assistant.Extension'
_EXTENSION_OBJECT_PATH = '/org/jarvis/Assistant/Extension'
_EXTENSION_IFACE = 'org.jarvis.Assistant.Extension'


def _extension_call(method, params, reply_type, timeout_ms=10000):
    """Синхронный D-Bus вызов метода расширения GNOME Shell. Возвращает
    распакованный кортеж ответа, или None, если расширение недоступно."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            _EXTENSION_BUS_NAME,
            _EXTENSION_OBJECT_PATH,
            _EXTENSION_IFACE,
            method,
            params,
            GLib.VariantType.new(reply_type),
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        return result.unpack()
    except GLib.Error as e:
        _err(f'[jarvis] расширение не ответило ({method}): {e}')
        return None


def _extension_arrange(keep_classes, layout):
    """Просит расширение GNOME Shell закрыть все окна, кроме рабочих
    приложений, и разложить их по рабочим столам. В отличие от wmctrl
    видит и Wayland-нативные окна. Возвращает строку-итог, или None,
    если расширение недоступно (тогда используется wmctrl-фallback)."""
    result = _extension_call(
        'SetupWorkEnvironment',
        GLib.Variant('(a(assis),as)', (layout, keep_classes)),
        '(s)', 30000)
    return result[0] if result else None


def _extension_get_context():
    """Спрашивает расширение, что сейчас открыто на рабочем столе (активное
    окно + список окон по столам). Возвращает строку или None."""
    result = _extension_call('GetWindowContext', GLib.Variant('()', ()), '(s)')
    return result[0] if result else None


def _extension_capture_screen():
    """Делает скриншот через расширение (единственный легальный путь на
    Wayland). Возвращает путь к PNG или None."""
    result = _extension_call('CaptureScreen', GLib.Variant('()', ()), '(s)', 15000)
    path = result[0] if result else None
    if path and os.path.exists(path):
        return path
    return None


def _wmctrl_window_context():
    """Запасной вариант «что открыто» без расширения: только X11-окна
    (Wayland-нативные сюда не попадут)."""
    r = _run(['wmctrl', '-lx'])
    if r.returncode != 0 or not r.stdout.strip():
        return 'Не могу определить окна — расширение недоступно.'
    entries = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        entries.append(f'{parts[2]}: {parts[4]}')
    return 'Открыты окна (X11): ' + '; '.join(entries) if entries else 'Открытых окон нет.'


def action_start_work(_args):
    """Режим «начать работу»: открывает VS Code и Zen (если не запущены),
    закрывает все остальные окна и раскладывает рабочие приложения по
    разным рабочим столам (задаётся в WORK_APP_WORKSPACES)."""
    launched, already = [], []
    for app in WORK_APPS:
        if _app_windows(app['wm_classes']):
            already.append(app['name'])
        else:
            # Окна нет: запускаем приложение. Это покрывает и случай, когда
            # процесс жив, но окно закрыто, — command открывает свежее окно.
            _launch_desktop_app(app['desktops'], app['search'])
            launched.append(app['name'])

    parts = []
    if launched:
        parts.append(f'Открыл: {", ".join(launched)}.')
    if already:
        parts.append(f'{", ".join(already)} уже работают.')

    # Основной путь: всё делает расширение GNOME Shell (видит и
    # Wayland-окна). Если его нет — старый wmctrl-путь (только закрытие
    # X11-окон, без раскладки по столам).
    keep_classes = []
    for app in WORK_APPS:
        keep_classes += app['wm_classes']
    layout = []
    for app in WORK_APPS:
        ws_num = WORK_APP_WORKSPACES.get(app['name'])
        if ws_num is None:
            continue
        layout.append((app['wm_classes'], app['processes'], ws_num, app['name']))

    summary = _extension_arrange(keep_classes, layout)
    if summary:
        parts.append(summary)
    else:
        closed = _close_other_windows(keep_classes)
        if closed:
            parts.append(f'Закрыл {closed} посторонних окон.')

    return ' '.join(parts) or 'Всё готово к работе.'


# ============================== ЗРЕНИЕ: ЭКРАН ==============================
# «Ева, что на экране?» — скриншот делает расширение GNOME Shell (на Wayland
# это единственный легальный путь), затем картинку описывает облачная
# vision-модель (см. VISION_OPENAI_MODEL).

VISION_PROMPT = ('Кратко опиши, что видно на скриншоте рабочего стола '
                 'пользователя: какое окно главное, что на нём, сколько '
                 'окон видно. 3-5 предложений, на русском.')


def _vision_openai(image_path):
    """Описание скриншота облачной моделью (OpenAI-совместимый API)."""
    with open(image_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    url = OPENAI_BASE_URL.rstrip('/') + '/chat/completions'
    headers = {}
    if OPENAI_API_KEY:
        headers['Authorization'] = f'Bearer {OPENAI_API_KEY}'
    payload = {
        'model': VISION_OPENAI_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': VISION_PROMPT},
                {'type': 'image_url',
                 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ],
        }],
        'max_tokens': 300,
    }
    resp = _http.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    text = ((resp.json().get('choices') or [{}])[0]
            .get('message', {}).get('content') or '').strip()
    return text or None


def describe_screen():
    """Скриншот + описание vision-моделью. Возвращает текст ответа,
    или None, если бэкенд не сработал (тогда ассистент отвечает что-то
    вроде «не могу увидеть экран»)."""
    path = _extension_capture_screen()
    if not path:
        _err('[jarvis] скриншот не получен — расширение недоступно')
        return None

    try:
        text = _vision_openai(path)
        if text:
            _log('[vision] описал скриншот (openai)')
            return text
    except Exception as e:
        _err(f'[vision] облачная модель не описала экран: {e}')
    return None


def action_see_screen(_args):
    """Показывает LLM, что сейчас на экране: делает скриншот и возвращает
    его описание (vision-моделью)."""
    text = describe_screen()
    if text is None:
        return ('Не получилось посмотреть на экран: нет vision-модели. '
                'Задайте ключ и vision-модель в настройках '
                '(OPENAI_API_KEY и VISION_OPENAI_MODEL).')
    return text


def action_get_window_context(_args):
    """Список открытых окон и активного окна (без картинки, быстро)."""
    ctx = _extension_get_context()
    if ctx is None:
        ctx = _wmctrl_window_context()
    return ctx


# --- голосовые запросы «что на экране» обрабатываются до роутера/LLM ---

_VISION_RE = re.compile(
    r'^(?:что\s+)?на\s+экране'
    r'|^опиши\s+(?:экран|рабочий\s+стол|что\s+на\s+экране)'
    r'|^посмотри\s+что\s+на\s+экране'
    r'|^что\s+ты\s+видиш\w*$',
    re.I)


def is_vision_request(text):
    return bool(_VISION_RE.match(text.strip()))


def run_vision_flow():
    """Полный цикл «посмотреть на экран»: скриншот → vision-описание → озвучка."""
    _log('[jarvis] vision-запрос: смотрю на экран...')
    emit_state('thinking')
    text = describe_screen()
    if text is None:
        text = ('Извините, мне пока нечем смотреть на экран: не найдено '
                'vision-модели. Подробности в логах.')
    _log(f'[jarvis] ответ (vision): {text!r}')
    emit_response(text)
    emit_state('speaking')
    try:
        speak(text)
    except Exception as e:
        _log(f'[jarvis] ошибка озвучки: {e}')
    emit_state('idle')
    return 'done'


def action_set_volume(args):
    try:
        percent = max(0, min(100, int(args.get('percent'))))
    except (TypeError, ValueError):
        return 'Не понял, на сколько процентов выставить громкость.'
    r = _run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{percent}%'])
    if r.returncode != 0:
        return f'Не удалось изменить громкость: {r.stderr.strip()}'
    return f'Громкость выставлена на {percent}%.'


def action_set_mute(args):
    mute = bool(args.get('mute', True))
    r = _run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', '1' if mute else '0'])
    if r.returncode != 0:
        return f'Не удалось изменить звук: {r.stderr.strip()}'
    return 'Звук выключен.' if mute else 'Звук включён.'


def action_set_brightness(args):
    try:
        percent = max(1, min(100, int(args.get('percent'))))
    except (TypeError, ValueError):
        return 'Не понял, на сколько процентов выставить яркость.'
    if not shutil.which('brightnessctl'):
        return 'Утилита brightnessctl не установлена — не могу управлять яркостью.'
    r = _run(['brightnessctl', 'set', f'{percent}%'])
    if r.returncode != 0:
        return f'Не удалось изменить яркость: {r.stderr.strip()}'
    return f'Яркость выставлена на {percent}%.'


def action_set_wifi(args):
    state = (args.get('state') or '').strip().lower()
    if state not in ('on', 'off'):
        return 'Не понял, включить Wi-Fi или выключить.'
    if not shutil.which('nmcli'):
        return 'NetworkManager (nmcli) не найден — не могу управлять Wi-Fi.'
    r = _run(['nmcli', 'radio', 'wifi', state])
    if r.returncode != 0:
        return f'Не удалось переключить Wi-Fi: {r.stderr.strip()}'
    return 'Wi-Fi включён.' if state == 'on' else 'Wi-Fi выключен.'

def action_lock_screen(_args):
    r = _run([
        'gdbus', 'call', '--session',
        '--dest', 'org.gnome.ScreenSaver',
        '--object-path', '/org/gnome/ScreenSaver',
        '--method', 'org.gnome.ScreenSaver.Lock',
    ])
    if r.returncode != 0:
        return f'Не удалось заблокировать экран: {r.stderr.strip()}'
    return 'Экран заблокирован.'


def action_suspend(_args):
    r = _run(['systemctl', 'suspend'])
    if r.returncode != 0:
        return f'Не удалось перейти в спящий режим: {r.stderr.strip()}'
    return 'Перевожу ноутбук в спящий режим.'


def action_set_dark_mode(args):
    enabled = bool(args.get('enabled', True))
    value = 'prefer-dark' if enabled else 'default'
    r = _run(['gsettings', 'set', 'org.gnome.desktop.interface', 'color-scheme', value])
    if r.returncode != 0:
        return f'Не удалось переключить тему: {r.stderr.strip()}'
    return 'Включил тёмную тему.' if enabled else 'Включил светлую тему.'


def action_set_night_light(args):
    enabled = bool(args.get('enabled', True))
    r = _run([
        'gsettings', 'set', 'org.gnome.settings-daemon.plugins.color',
        'night-light-enabled', 'true' if enabled else 'false',
    ])
    if r.returncode != 0:
        return f'Не удалось переключить ночной режим: {r.stderr.strip()}'
    return 'Ночной режим включён.' if enabled else 'Ночной режим выключен.'


def action_open_url(args):
    url = (args.get('url') or '').strip()
    if not url or url in ('https://', 'http://'):
        return 'Не указан адрес для открытия.'
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    r = _run(['xdg-open', url])
    if r.returncode != 0:
        return f'Не удалось открыть ссылку: {r.stderr.strip()}'
    return f'Открываю {url}.'


def action_get_datetime(_args):
    now = datetime.datetime.now()
    return now.strftime('Сейчас %H:%M, %d.%m.%Y.')


# --- Таймер ---

_timer_lock = threading.Lock()
_timer_count = 0


def _timer_fire(duration, text):
    time.sleep(duration)
    _log(f'[jarvis] таймер вышел: {text}')
    try:
        speak(text)
    except Exception as e:
        _log(f'[jarvis] не удалось озвучить таймер: {e}')


def action_set_timer(args):
    """Запускает фоновый таймер: через заданное время Ева произнесёт
    «Таймер вышел!»."""
    global _timer_count
    try:
        minutes = max(0, int(args.get('minutes') or 0))
        seconds = max(0, int(args.get('seconds') or 0))
    except (TypeError, ValueError):
        return 'Не понял, на какое время поставить таймер.'
    duration = minutes * 60 + seconds
    if duration == 0:
        return 'Не понял, на какое время поставить таймер. Скажите, например: таймер на 5 минут.'
    if duration < TIMER_MIN_SECONDS:
        return 'Таймер слишком короткий. Поставьте хотя бы несколько секунд.'
    if duration > TIMER_MAX_SECONDS:
        return 'Таймер слишком длинный. Максимум 6 часов.'
    with _timer_lock:
        _timer_count += 1
        num = _timer_count
    text = 'Таймер вышел!'
    threading.Thread(target=_timer_fire, args=(duration, text), daemon=True).start()
    if minutes and seconds:
        return f'Таймер на {minutes} минут {seconds} секунд запущен.'
    if minutes:
        return f'Таймер на {minutes} минут запущен.'
    return f'Таймер на {seconds} секунд запущен.'


# --- Поиск файлов ---

def action_find_files(args):
    """Ищет файлы в домашнем каталоге по имени (до 5 результатов)."""
    name = (args.get('name') or '').strip()
    if not name:
        return 'Не указано, что искать.'
    # Ищем find-ом: locate-база может быть устаревшей (обновляется ночью),
    # а find с maxdepth работает мгновенно
    cmd = ['find', os.path.expanduser('~'), '-maxdepth', '5',
           '-iname', f'*{name}*', '-not', '-path', '*/.cache/*',
           '-not', '-path', '*/.local/share/Trash/*']
    try:
        # rc=1 возможен из-за недоступных каталогов — stdout при этом валиден
        r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, timeout=15,
                           stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 'Поиск занял слишком много времени.'
    if r.returncode not in (0, 1):
        return 'Поиск завершился с ошибкой.'
    paths = [p for p in r.stdout.splitlines() if p.strip()][:5]
    if not paths:
        return f'Ничего не нашёл по запросу "{name}".'
    short = [os.path.expanduser(p) for p in paths]
    return 'Нашёл: ' + ', '.join(short)


# --- Погода (open-meteo, без ключа) ---

_WEATHER_CODES = {
    0: 'ясно', 1: 'почти ясно', 2: 'переменная облачность', 3: 'пасмурно',
    45: 'туман', 48: 'изморозь', 51: 'лёгкая морось', 53: 'морось',
    55: 'сильная морось', 56: 'ледяная морось', 57: 'сильная ледяная морось',
    61: 'небольшой дождь', 63: 'дождь', 65: 'сильный дождь',
    66: 'ледяной дождь', 67: 'сильный ледяной дождь',
    71: 'небольшой снег', 73: 'снег', 75: 'сильный снег', 77: 'снежная крупа',
    80: 'небольшие ливни', 81: 'ливни', 82: 'сильные ливни',
    85: 'небольшой снегопад', 86: 'сильный снегопад',
    95: 'гроза', 96: 'гроза с градом', 99: 'сильная гроза с градом',
}


def _weather_desc(code):
    return _WEATHER_CODES.get(code, 'неизвестная погода')


def action_get_weather(args):
    if not WEATHER_LAT and not WEATHER_LON:
        return 'Геопозиция не настроена. Впишите координаты города в конфиг демона.'
    try:
        r = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'temperature_2m,weather_code,wind_speed_10m',
                'daily': 'temperature_2m_max,temperature_2m_min,weather_code',
                'timezone': 'auto',
                'forecast_days': 2,
            },
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        _err(f'[action:get_weather] ошибка: {e}')
        return 'Не удалось получить прогноз погоды.'
    data = r.json()
    cur = data.get('current', {})
    daily = data.get('daily', {})
    today_max = (daily.get('temperature_2m_max') or [None])[0]
    today_min = (daily.get('temperature_2m_min') or [None])[0]
    text = (f'Сейчас {cur.get("temperature_2m", "?")} градусов, '
            f'{_weather_desc(cur.get("weather_code", 0))}. ')
    if today_max is not None:
        text += (f'Сегодня от {today_min} до {today_max} градусов. ')
    tomorrow_max = (daily.get('temperature_2m_max') or [None, None])[1]
    if tomorrow_max is not None:
        code = (daily.get('weather_code') or [0, 0])[1]
        text += f'Завтра около {tomorrow_max} градусов, {_weather_desc(code)}.'
    return text


# --- Курсы валют (open.er-api.com, бесплатно без ключа) ---

def action_get_rates(_args):
    """Актуальные курсы валют к рублю (бесплатный API, без ключа).
    Возвращает фразу для озвучки или '' при ошибке/оффлайне."""
    try:
        r = requests.get('https://open.er-api.com/v6/latest/USD', timeout=10)
        r.raise_for_status()
        rates = (r.json() or {}).get('rates') or {}
    except (requests.RequestException, ValueError) as e:
        _err(f'[action:get_rates] ошибка: {e}')
        return ''
    usd_rub = rates.get('RUB')
    if not usd_rub:
        return ''
    parts = []
    for code, name in RATES_CURRENCIES:
        if code == 'USD':
            parts.append(f'{name} — {usd_rub:.0f} рублей')
        elif rates.get(code):
            parts.append(f'{name} — {usd_rub / rates[code]:.0f} рублей')
    return 'Курсы: ' + ', '.join(parts) + '.'


# --- Новости (Google News RSS, бесплатно без ключа) ---

def action_get_news(_args):
    """Короткая сводка новостей из RSS-лент (см. NEWS_RSS_URLS — ленты
    пробуются по очереди). Возвращает фразу для озвучки или '' при
    ошибке/оффлайне."""
    for feed_url in NEWS_RSS_URLS:
        try:
            r = requests.get(feed_url,
                             headers={'User-Agent': 'Mozilla/5.0'},
                             timeout=8)
            r.raise_for_status()
        except requests.RequestException as e:
            _err(f'[action:get_news] лента {feed_url}: {e}')
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            _err(f'[action:get_news] не удалось разобрать {feed_url}: {e}')
            continue
        titles = []
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            if title:
                titles.append(title[:100])
            if len(titles) >= NEWS_HEADLINES:
                break
        if titles:
            return 'Новости: ' + '. '.join(titles) + '.'
    return ''


# --- ПРОТОКОЛ «Утро / Старт дня» ---

def _launch_morning_app(item):
    """Открывает утреннее приложение: установленное — по названию,
    иначе веб-версию по ссылке. Возвращает True, если что-то открыли."""
    result = ''
    if item.get('app'):
        result = action_open_app({'name': item['app']})
    if result and not result.startswith('Не нашёл приложение'):
        return True
    if item.get('url'):
        action_open_url({'url': item['url']})
        return True
    return False


def _morning_summary():
    """Сводка утра: дата + погода + курсы валют + новости. Запросы
    идут параллельно — суммарно ~2-3 с."""

    def _safe(fn, out, idx):
        try:
            out[idx] = fn({})
        except Exception as e:
            _err(f'[morning] {fn.__name__}: {e}')
            out[idx] = ''

    outs = ['', '', '']
    jobs = [(action_get_weather, 0), (action_get_rates, 1), (action_get_news, 2)]
    threads = [threading.Thread(target=_safe, args=(fn, outs, i), daemon=True)
               for fn, i in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    weather = outs[0]
    if 'градусов' not in weather:
        weather = ''  # геопозиция не настроена или API недоступен
    return ' '.join(x for x in [_now_date(), weather, outs[1], outs[2]] if x)


def _morning_music():
    """Фоновая музыка/подкаст: приложение, если установлено, иначе
    веб-плеер; громкость выставляется под фон. Возвращает фразу."""
    try:
        action_set_volume({'percent': MORNING_MUSIC_VOLUME})
    except Exception as e:
        _err(f'[morning] громкость: {e}')
    if MORNING_MUSIC_APP:
        try:
            result = action_open_app({'name': MORNING_MUSIC_APP})
        except Exception as e:
            _err(f'[morning] {MORNING_MUSIC_APP}: {e}')
            result = 'Не нашёл приложение'
        if result and not result.startswith('Не нашёл приложение'):
            return 'Включил фоновую музыку.'
    if MORNING_MUSIC_URL:
        try:
            action_open_url({'url': MORNING_MUSIC_URL})
            return 'Включил фоновую музыку.'
        except Exception as e:
            _err(f'[morning] музыка: {e}')
    return ''


def action_morning_routine(_args):
    """ПРОТОКОЛ «Утро / Старт дня»: открывает почту, календарь и
    таск-менеджер, зачитывает сводку (погода, курсы валют, новости)
    и включает фоновую музыку на негромкой громкости."""
    parts = []

    opened = []
    for item in MORNING_APPS:
        if _launch_morning_app(item):
            opened.append(item.get('app') or item['url'])
    if opened:
        parts.append('Открыл: ' + ', '.join(opened) + '.')

    summary = _morning_summary()
    if summary:
        parts.append(summary)

    music = _morning_music()
    if music:
        parts.append(music)

    return ' '.join(parts) or 'Не получилось выполнить утренний протокол.'


# --- ПРОТОКОЛ «Исследование / Обучение» ---

def _open_notes_app():
    """Открывает приложение для заметок: первое установленное из списка,
    иначе первую веб-версию. Возвращает фразу или ''."""
    for item in LEARNING_NOTES_APPS:
        app_name = item.get('app')
        if app_name:
            try:
                result = action_open_app({'name': app_name})
            except Exception as e:
                _err(f'[learning] заметки {app_name}: {e}')
                result = 'Не нашёл приложение'
            if result and not result.startswith('Не нашёл приложение'):
                return f'Открыл заметки: {app_name}.'
    for item in LEARNING_NOTES_APPS:
        if item.get('url'):
            try:
                action_open_url({'url': item['url']})
                return 'Открыл заметки в браузере.'
            except Exception as e:
                _err(f'[learning] заметки: {e}')
    return ''


def action_learning_routine(_args):
    """ПРОТОКОЛ «Исследование / Обучение»: открывает браузер с вкладками
    (документация, Stack Overflow, YouTube), заметки (Obsidian/блокнот) и
    терминал для экспериментов."""
    parts = []

    opened_tabs = 0
    for url in LEARNING_TABS:
        try:
            action_open_url({'url': url})
            opened_tabs += 1
        except Exception as e:
            _err(f'[learning] вкладка {url}: {e}')
    if opened_tabs:
        parts.append(f'Открыл {opened_tabs} вкладки браузера: документация, '
                     f'Stack Overflow и YouTube.')

    notes = _open_notes_app()
    if notes:
        parts.append(notes)

    try:
        result = action_open_app({'name': 'терминал'})
        if result and not result.startswith('Не нашёл приложение'):
            parts.append('Открыл терминал для экспериментов.')
    except Exception as e:
        _err(f'[learning] терминал: {e}')

    return ' '.join(parts) or 'Не получилось выполнить протокол исследования.'


# --- ПРОТОКОЛ «Коммуникация» ---

_tg_ws_proxy_proc = None


def _tg_ws_proxy_running(port):
    """Отвечает ли порт TCP: прокси уже поднят (этим демоном, вручную
    или через systemd) — второй поднимать не нужно."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except OSError:
            return False


def _ensure_tg_ws_proxy():
    """Поднимает tg-ws-proxy как фоновый процесс — скриптом, без systemd
    и диалога пароля (пользовательский процесс, слушает 127.0.0.1).
    Возвращает фразу-статус."""
    global _tg_ws_proxy_proc
    port = TG_WSPROXY_PORT
    if _tg_ws_proxy_proc is not None:
        if _tg_ws_proxy_proc.poll() is not None:
            _tg_ws_proxy_proc = None
        else:
            return 'Прокси уже работает.'
    if _tg_ws_proxy_running(port):
        return 'Прокси уже работает.'
    log = os.path.expanduser('~/.local/share/jarvis-assistant/'
                             'tg-ws-proxy.log')
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = ['tg-ws-proxy', '--port', str(port)]
    if TG_WSPROXY_SECRET:
        cmd += ['--secret', TG_WSPROXY_SECRET]
    cmd += ['--log-file', log]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as e:
        return f'Не удалось запустить прокси: {e}.'
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if _tg_ws_proxy_running(port):
            _tg_ws_proxy_proc = proc
            _log(f'[jarvis] tg-ws-proxy поднят на 127.0.0.1:{port}')
            return 'Поднял прокси для Telegram.'
        if proc.poll() is not None:
            return 'Прокси не запустился — проверьте его лог.'
        time.sleep(0.2)
    return 'Прокси не успел подняться — проверьте его лог.'


def _push_tg_proxy_link():
    """Отправляет Telegram команду добавить прокси (tg://proxy). Делается
    скриптом, без перезапуска приложения: если Telegram уже запущен — ссылка
    уходит в работающее окно (останется нажать «Подключить»); если закрыт —
    пропускаем, чтобы не разворачивать приложение только ради этого."""
    link = ('tg://proxy?server=127.0.0.1&port=%d&secret=%s'
            % (TG_WSPROXY_PORT, TG_WSPROXY_SECRET))
    try:
        r = subprocess.run(['pgrep', '-f', 'org.telegram.desktop'],
                           capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return ''
    if r.returncode != 0:
        return ('Telegram сейчас закрыт — прокси подключится, '
                'когда вы его откроете.')
    for opener in (['xdg-open', link],
                   ['flatpak', 'run', 'org.telegram.desktop', link]):
        try:
            rr = subprocess.run(opener, capture_output=True, text=True,
                                timeout=8)
            if rr.returncode == 0:
                return ('Добавил прокси в Telegram — '
                        'подтвердите подключение.')
        except (OSError, subprocess.TimeoutExpired):
            continue
    return 'Не смог отправить прокси в Telegram.'


def _check_unread():
    """Проверка непрочитанных/приоритетных сообщений скриптом-хуком
    UNREAD_CHECK_CMD. Возвращает фразу для озвучки или '' (шаг выключен)."""
    if not UNREAD_CHECK_CMD:
        return ''
    try:
        r = subprocess.run(UNREAD_CHECK_CMD, shell=True, capture_output=True,
                           text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return 'Проверка сообщений заняла слишком много времени.'
    text = (r.stdout or '').strip()
    if not text:
        return 'Непрочитанных сообщений нет.'
    return text[:400]


def action_communication_routine(_args):
    """ПРОТОКОЛ «Коммуникация»: поднимает MTProto-прокси для Telegram
    (скриптом, без запуска приложения ради этого), открывает мессенджеры
    (Telegram/Slack/Discord) и проверяет непрочитанные сообщения."""
    parts = []

    parts.append(_ensure_tg_ws_proxy())

    opened, missing = [], []
    for item in COMMUNICATION_APPS:
        app_name = item.get('app')
        if not app_name:
            continue
        result = action_open_app({'name': app_name})
        if result.startswith('Не нашёл приложение'):
            if item.get('url'):
                action_open_url({'url': item['url']})
                opened.append(app_name)
            else:
                missing.append(app_name)
        else:
            opened.append(app_name)
    if opened:
        parts.append('Открыл: ' + ', '.join(opened) + '.')
    if missing:
        parts.append(f'Не установлены: {", ".join(missing)}.')

    if 'telegram' in opened:
        proxy_note = _push_tg_proxy_link()
        if proxy_note:
            parts.append(proxy_note)

    unread = _check_unread()
    if unread:
        parts.append(unread)
    else:
        parts.append('Непрочитанные проверить не могу — нет доступа '
                     'к API мессенджеров. Настройте скрипт UNREAD_CHECK_CMD '
                     'в конфиге, если хотите, чтобы я их читала.')

    return ' '.join(parts)


# --- Скриншот ---

def _portal_screenshot():
    """Скриншот через xdg-desktop-portal — единственный рабочий способ
    на GNOME Wayland (50+). В первый раз GNOME покажет диалог разрешения
    один раз, после этого снимает автоматически. Возвращает путь к файлу
    (~/Pictures) или None."""
    token = 'jv%d' % int(time.monotonic())
    opts = ('{{"handle_token": <"{t}">, "modal": <false>, '
            '"interactive": <true>}}').format(t=token)
    subprocess.Popen(['gdbus', 'call', '--session',
                      '--dest', 'org.freedesktop.portal.Desktop',
                      '--object-path', '/org/freedesktop/portal/desktop',
                      '--method', 'org.freedesktop.portal.Screenshot.Screenshot',
                      '', opts],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = subprocess.run(['gdbus', 'monitor', '--session',
                            '--dest', 'org.freedesktop.portal.Desktop'],
                           capture_output=True, text=True, timeout=20)
        out = r.stdout
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


def action_take_screenshot(_args):
    """Скриншот: gnome-screenshot → grim → import (X11) → портал (Wayland)."""
    out_dir = os.path.expanduser('~/Изображения')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir,
                        'screenshot_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.png')

    if shutil.which('gnome-screenshot'):
        r = _run(['gnome-screenshot', '-f', path])
        if r.returncode == 0:
            return f'Скриншот сохранён: {path}.'
    elif shutil.which('grim'):
        r = _run(['grim', path])
        if r.returncode == 0:
            return f'Скриншот сохранён: {path}.'
    elif shutil.which('import'):
        r = _run(['import', '-window', 'root', path])
        if r.returncode == 0:
            return f'Скриншот сохранён: {path}.'

    portal_path = _portal_screenshot()
    if portal_path:
        return f'Скриншот сохранён: {portal_path}.'
    return ('Не удалось сделать скриншот. Установите grim '
            '(sudo pacman -S grim) или разрешите в появившемся окне.')


ACTIONS = {
    'start_work': action_start_work,
    'see_screen': action_see_screen,
    'get_window_context': action_get_window_context,
    'open_app': action_open_app,
    'set_volume': action_set_volume,
    'set_mute': action_set_mute,
    'set_brightness': action_set_brightness,
    'set_wifi': action_set_wifi,
    'lock_screen': action_lock_screen,
    'suspend': action_suspend,
    'set_dark_mode': action_set_dark_mode,
    'set_night_light': action_set_night_light,
    'open_url': action_open_url,
    'get_datetime': action_get_datetime,
    'set_timer': action_set_timer,
    'find_files': action_find_files,
    'get_weather': action_get_weather,
    'get_rates': action_get_rates,
    'get_news': action_get_news,
    'morning_routine': action_morning_routine,
    'learning_routine': action_learning_routine,
    'communication_routine': action_communication_routine,
    'take_screenshot': action_take_screenshot,
}

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'see_screen',
            'description': 'Посмотреть на рабочий стол: делает скриншот и описывает его vision-моделью. Используй, когда спрашивают «что на экране», «посмотри», «прочитай что-то с экрана» или просят описать содержимое окна/браузера.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_window_context',
            'description': 'Узнать, что сейчас открыто на рабочем столе: активное окно, список открытых окон по рабочим столам. Быстро, без картинок. Используй на вопросы «что открыто», «какие окна», «на каком я столе».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'start_work',
            'description': 'Режим «начать работу»: открывает рабочие приложения (редактор кода VS Code и браузер Zen), если они ещё не запущены, и закрывает все остальные окна на рабочем столе.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'morning_routine',
            'description': 'ПРОТОКОЛ «Утро / Старт дня»: открывает почту, календарь и таск-менеджер (Todoist/Notion), зачитывает сводку — погода, курсы валют, новости — и включает фоновую музыку. Используй на «доброе утро», «начни день», «утренняя сводка», «план на день», «старт дня».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'learning_routine',
            'description': 'ПРОТОКОЛ «Исследование / Обучение»: открывает браузер с вкладками (документация, Stack Overflow, YouTube), заметки (Obsidian/блокнот) и терминал для экспериментов. Используй на «режим исследования», «давай учиться», «хочу позаниматься», «начни исследование».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_rates',
            'description': 'Узнать актуальные курсы валют к рублю (доллар, евро, юань). Вопросы «курс доллара», «сколько стоит евро», «какие курсы сегодня».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_news',
            'description': 'Короткая сводка новостей — первые заголовки из ленты. Вопросы «что нового», «какие новости», «что происходит в мире».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'open_app',
            'description': 'Открыть установленное приложение на компьютере по названию (браузер, терминал, файлы, настройки, калькулятор, firefox, telegram и т.п.). Используй для приложений, а не для веб-адресов — для ссылок есть open_url.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': 'Название приложения'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_volume',
            'description': 'Установить громкость системного звука в процентах (0-100).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'percent': {'type': 'integer', 'description': 'Громкость от 0 до 100'},
                },
                'required': ['percent'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_mute',
            'description': 'Выключить или включить звук (mute/unmute).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'mute': {'type': 'boolean', 'description': 'true — выключить звук, false — включить'},
                },
                'required': ['mute'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_brightness',
            'description': 'Установить яркость экрана ноутбука в процентах (1-100).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'percent': {'type': 'integer', 'description': 'Яркость от 1 до 100'},
                },
                'required': ['percent'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_wifi',
            'description': 'Включить или выключить Wi-Fi.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'state': {'type': 'string', 'enum': ['on', 'off']},
                },
                'required': ['state'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'lock_screen',
            'description': 'Заблокировать экран компьютера.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'suspend',
            'description': 'Перевести ноутбук в спящий режим.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_dark_mode',
            'description': 'Включить или выключить тёмную тему оформления GNOME.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'enabled': {'type': 'boolean', 'description': 'true — тёмная тема, false — светлая'},
                },
                'required': ['enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_night_light',
            'description': 'Включить или выключить ночной режим экрана (тёплые тона).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'enabled': {'type': 'boolean'},
                },
                'required': ['enabled'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'open_url',
            'description': 'Открыть веб-сайт по адресу в браузере по умолчанию. Только для ссылок/сайтов; для локальных приложений используй open_app.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'Адрес сайта, например habr.com'},
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_datetime',
            'description': 'Узнать текущую дату и время.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_timer',
            'description': 'Поставить таймер: через указанное время ассистент сам скажет «Таймер вышел». Например «таймер на 5 минут» — minutes=5, seconds=0.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'minutes': {'type': 'integer', 'description': 'Минуты (0-360)'},
                    'seconds': {'type': 'integer', 'description': 'Секунды (0-59)'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_files',
            'description': 'Найти файл на компьютере по имени или части имени (например «презентация», «отчёт.pdf»). Возвращает до 5 найденных путей.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': 'Имя или часть имени файла'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_weather',
            'description': 'Узнать текущую погоду и прогноз на завтра в городе пользователя.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'take_screenshot',
            'description': 'Сделать скриншот экрана и сохранить в папку «Изображения».',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
]


# Дедупликация действий: модель может вызвать одно и то же действие
# несколько раз за один запрос (или повторить вызов на следующих кругах
# tools) — выполняем только первым. Сбрасывается на каждую команду
# в run_command_flow.
_action_done = set()
_action_lock = threading.Lock()


def execute_tool(name, arguments):
    if not ALLOW_SYSTEM_ACTIONS:
        return 'Управление системой отключено в настройках ассистента.'
    fn = ACTIONS.get(name)
    if not fn:
        return f'Неизвестное действие: {name}'
    key = (name, json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False))
    with _action_lock:
        if key in _action_done:
            return 'Уже выполнено.'
        _action_done.add(key)
    try:
        result = fn(arguments or {})
    except subprocess.TimeoutExpired:
        result = f'Действие {name} не выполнилось за отведённое время.'
    except Exception as e:
        _err(f'[action:{name}] ошибка: {e}')
        result = f'Не получилось выполнить действие.'
    # НЕ озвучиваем результат здесь: финальный ответ модели (который
    # озвучивается в run_command_flow) и так коротко подтвердит действие —
    # иначе одно и то же звучит дважды («Открываю браузер» + «Я открыла браузер»).
    return result


# ============================== БЫСТРЫЙ РОУТЕР ДЕЙСТВИЙ =====================
#
# Ключевая идея: большинство голосовых команд («открой браузер», «громкость
# на 50», «покажи фото кота», «включи тёмную тему») не требуют «ума» LLM —
# это однозначное сопоставление фразы и системного действия. Раньше такие
# команды всё равно шли в LLM ради function calling, а это 2-15 секунд
# ожидания даже для тривиального «открой терминал».
#
# Этот роутер разбирает текст локальными regex'ами и, если понял команду
# однозначно, выполняет её НАПРЯМУЮ, без единого сетевого запроса —
# распознанное действие срабатывает за десятки миллисекунд. Если ни один
# паттерн не подошёл — текст уходит дальше по обычному пути (шорткаты,
# затем LLM с function calling), так что сложные и неоднозначные фразы
# по-прежнему обрабатываются моделью.
#
# Правило добавления новых паттернов: матчить только то, что понимается
# однозначно. Лучше пропустить сомнительную фразу в LLM, чем один раз
# уверенно сделать не то действие.

def _search_url(query, kind='web'):
    q = urllib.parse.quote(query.strip())
    if kind == 'images':
        return f'https://www.google.com/search?tbm=isch&q={q}'
    if kind == 'video':
        return f'https://www.youtube.com/results?search_query={q}'
    return f'https://www.google.com/search?q={q}'


def action_adjust_volume(delta):
    """delta вида '+10%' / '-10%' — pactl понимает относительные проценты."""
    r = _run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', delta])
    return r.returncode == 0


def action_adjust_brightness(delta):
    """delta вида '10%+' / '10%-' — синтаксис brightnessctl."""
    if not shutil.which('brightnessctl'):
        return False
    r = _run(['brightnessctl', 'set', delta])
    return r.returncode == 0


_FAST_ROUTES = []  # (скомпилированный regex, обработчик) — проверяются по порядку


def _route(pattern):
    compiled = re.compile(pattern, re.I)

    def deco(fn):
        _FAST_ROUTES.append((compiled, fn))
        return fn
    return deco


# --- поиск (картинки/видео/веб) — САМЫЕ специфичные паттерны идут первыми,
#     чтобы «открой картинку кота» не улетело в общий open_app ниже ---

@_route(r'^(?:покажи|найди|открой)\s+(?:мне\s+)?(?:в браузере\s+)?'
        r'(?:картинку|картинки|фото|фотографию|изображение|изображения)\s+(.+)$')
def _r_image(m):
    query = m.group(1).strip()
    action_open_url({'url': _search_url(query, 'images')})
    return f'Ищу картинку: {query}.'


@_route(r'^(?:найди|включи|открой|покажи)\s+(?:мне\s+)?(?:видео|ролик)\s+'
        r'(?:про\s+|на тему\s+)?(.+)$')
def _r_video(m):
    query = m.group(1).strip()
    action_open_url({'url': _search_url(query, 'video')})
    return f'Ищу видео: {query}.'


@_route(r'^(?:найди|погугли|поищи)\s+(.+)$')
def _r_search(m):
    query = m.group(1).strip()
    action_open_url({'url': _search_url(query, 'web')})
    return f'Ищу: {query}.'


@_route(r'^(?:открой|зайди на|перейди на)\s+(?:сайт\s+)?'
        r'((?:https?://)?[a-zа-яё0-9\-]+\.[a-zа-яё]{2,}(?:/\S*)?)$')
def _r_url(m):
    return action_open_url({'url': m.group(1).strip()})


# --- громкость / звук ---

@_route(r'^(?:громкость|звук)\s*(?:на|)\s*(\d{1,3})\s*(?:процент\w*)?$')
def _r_volume_abs(m):
    return action_set_volume({'percent': int(m.group(1))})


@_route(r'^(?:сделай|сделать)?\s*(?:по)?громче$')
def _r_volume_up(m):
    action_adjust_volume('+10%')
    return 'Сделал громче.'


@_route(r'^(?:сделай|сделать)?\s*(?:по)?тише$')
def _r_volume_down(m):
    action_adjust_volume('-10%')
    return 'Сделал тише.'


@_route(r'^(?:выключи|отключи)\s+звук$')
def _r_mute(m):
    return action_set_mute({'mute': True})


@_route(r'^включи звук$')
def _r_unmute(m):
    return action_set_mute({'mute': False})


# --- яркость ---

@_route(r'^яркость\s*(?:на|)\s*(\d{1,3})\s*(?:процент\w*)?$')
def _r_bright_abs(m):
    return action_set_brightness({'percent': int(m.group(1))})


@_route(r'^(?:сделай|сделать)?\s*(?:по)?ярче$')
def _r_bright_up(m):
    action_adjust_brightness('10%+')
    return 'Сделал ярче.'


@_route(r'^(?:сделай|сделать)?\s*(?:по)?темнее$')
def _r_bright_down(m):
    action_adjust_brightness('10%-')
    return 'Сделал темнее.'


# --- wi-fi / тема / ночной режим / блокировка / сон / скриншот / погода ---

@_route(r'^включи\s+(?:вай-?фай|wi-?fi|вайфай)$')
def _r_wifi_on(m):
    return action_set_wifi({'state': 'on'})


@_route(r'^(?:выключи|отключи)\s+(?:вай-?фай|wi-?fi|вайфай)$')
def _r_wifi_off(m):
    return action_set_wifi({'state': 'off'})


@_route(r'^включи тёмн\w+ тем\w*$')
def _r_dark_on(m):
    return action_set_dark_mode({'enabled': True})


@_route(r'^включи светл\w+ тем\w*$')
def _r_dark_off(m):
    return action_set_dark_mode({'enabled': False})


@_route(r'^включи ночн\w+ режим$')
def _r_night_on(m):
    return action_set_night_light({'enabled': True})


@_route(r'^выключи ночн\w+ режим$')
def _r_night_off(m):
    return action_set_night_light({'enabled': False})


@_route(r'^заблокируй\s+(?:экран|компьютер|ноутбук)$')
def _r_lock(m):
    return action_lock_screen({})


@_route(r'^(?:усыпи(?:\s+ноутбук)?|спящий режим)$')
def _r_suspend(m):
    return action_suspend({})


@_route(r'^(?:сделай|сними)\s+скриншот$')
def _r_screenshot(m):
    return action_take_screenshot({})


@_route(r'^(?:какая\s+)?погода(?:\s+сегодня)?$')
def _r_weather(m):
    return action_get_weather({})


# --- ПРОТОКОЛ «Утро / Старт дня»: почта + календарь + задачи, сводка,
#     музыка (см. action_morning_routine). Проверяется до общих команд. ---

@_route(r'^(?:доброе\s+утро|с\s+добрым\s+утром|утро|начни\s+день|'
        r'начать\s+день|старт\s+дня|утренняя\s+сводка|'
        r'сводка\s+(?:за|на)\s+день|(?:составь\s+)?план\s+на\s+день)$')
def _r_morning(m):
    return action_morning_routine({})


# --- ПРОТОКОЛ «Исследование / Обучение»: браузер (документация + Stack
#     Overflow + YouTube), заметки, терминал (см. action_learning_routine) ---

@_route(r'^(?:режим\s+исследования|исследование|начни\s+исследование|'
        r'давай\s+учиться|учиться|хочу\s+позаниматься|режим\s+обучения|'
        r'обучение)$')
def _r_learning(m):
    return action_learning_routine({})


# --- «начать работу»: VS Code + Zen, остальные окна закрыть ---

@_route(r'^(?:начни|запусти)\s+работ\w+|^рабочий\s+режим|^режим\s+работы$')
def _r_start_work(m):
    return action_start_work({})


# --- «что открыто»: контекст окон — мгновенно, без LLM ---


@_route(r'^(?:что|какие)\s+(?:сейчас\s+)?(?:открыт\w*|запущен\w*|'
        r'приложения\s+открыт\w*|окна\s+открыт\w*)$')
@_route(r'^на\s+каком\s+рабочем\s+столе$')
@_route(r'^какой\s+рабочий\s+стол(?:\s+активен)?$')
@_route(r'^в\s+каком\s+(?:я\s+)?окне$')
def _r_window_context(m):
    ctx = _extension_get_context()
    if ctx is None:
        ctx = _wmctrl_window_context()
    return ctx


# --- общий запуск приложений — самый последний паттерн: широкий, поэтому
#     проверяется только когда ни один из специфичных выше не подошёл ---

@_route(r'^(?:открой|запусти|включи)\s+([a-zа-яё0-9 ]{2,25})$')
def _r_open_app(m):
    return action_open_app({'name': m.group(1).strip()})


def find_fast_route(text):
    """Пытается выполнить команду локально, без LLM. Возвращает готовую
    фразу для озвучки, если команда однозначно распознана и выполнена,
    иначе None (тогда команда идёт дальше — в шорткаты/LLM)."""
    if not ALLOW_SYSTEM_ACTIONS:
        return None
    t = text.strip(' ,.!?…').lower()
    if not t:
        return None
    for pattern, handler in _FAST_ROUTES:
        m = pattern.match(t)
        if not m:
            continue
        try:
            return handler(m)
        except Exception as e:
            _log(f'[jarvis] ошибка в быстром роутере ({handler.__name__}): {e}')
            return None  # не смогли — пусть попробует LLM
    return None


# ============================== СЛОЖНЫЕ ЗАДАЧИ ==============================
#
# Для «напиши код», «сделай сайт» и т.п. ответ модели занимает заметно
# больше времени (особенно на слабом железе), и молчание в этот момент
# выглядит как зависание. Вместо этого сразу озвучиваем короткое
# подтверждение («озвучиваем мышление») и только затем ждём полный ответ.

_COMPLEX_VERBS = ('напиши', 'создай', 'разработай', 'сгенерируй', 'сверстай',
                   'реализуй', 'придумай', 'спроектируй', 'запрограммируй',
                   'добавь функцию', 'исправь код', 'почини код')
_COMPLEX_NOUNS = ('код', 'скрипт', 'программ', 'сайт', 'страниц', 'приложени',
                   'функци', 'алгоритм', 'класс', 'модул', 'бота', 'игру',
                   'статью', 'эссе', 'сценарий', 'презентаци', 'проект')

_THINKING_PHRASES = (
    'Хорошо, начинаю работать над этим.',
    'Понял, приступаю — это может занять некоторое время.',
    'Принято, сейчас подумаю над реализацией.',
    'Хорошо, разбираюсь с задачей.',
)


def is_complex_request(text):
    """True для задач, которые явно требуют «подумать» (написать код,
    сайт, текст и т.п.), а не выполнить одно системное действие."""
    t = text.lower()
    return (any(v in t for v in _COMPLEX_VERBS)
            and any(n in t for n in _COMPLEX_NOUNS))


def announce_thinking():
    """Короткая голосовая реплика-подтверждение перед долгим ответом LLM,
    чтобы не было ощущения, что ассистент завис. Не блокирует поток."""
    threading.Thread(
        target=lambda: speak(random.choice(_THINKING_PHRASES)),
        daemon=True,
    ).start()


# ============================== БЫСТРЫЕ ШОРТКАТЫ ==============================
#
# Частые простые фразы обрабатываются БЕЗ LLM — ответ за ~0.2 с вместо
# 2-15 с обращения к LLM. Список фраз → функция-ответ.

def _now_time():
    return datetime.datetime.now().strftime('Сейчас %H часов %M минут.')


_RU_MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')


def _now_date():
    now = datetime.datetime.now()
    return (f'Сегодня {now.day} {_RU_MONTHS[now.month - 1]} '
            f'{now.year} года.')


SHORTCUTS = [
    (('который час', 'сколько времени', 'сколько сейчас времени',
      'скажи время', 'какое сейчас время'), _now_time),
    (('какое сегодня число', 'какой сегодня день', 'какая сегодня дата',
      'скажи дату', 'какое сегодня число'), _now_date),
    (('привет', 'здравствуй', 'здравствуйте', 'добрый день', 'доброе утро',
      'добрый вечер', 'салют'), lambda: 'Привет! Чем могу помочь?'),
    (('спасибо', 'благодарю', 'большое спасибо'),
     lambda: 'Пожалуйста! Обращайтесь.'),
    (('пока', 'до свидания', 'до встречи'),
     lambda: 'До свидания! Буду рядом.'),
    # Разговорные фразы ниже тоже отвечаются на месте, без LLM
    (('как дела', 'как ты', 'как ты поживаешь', 'как жизнь'),
     lambda: 'Отлично, готова к работе. Что сделаем?'),
    (('кто ты', 'как тебя зовут', 'ты кто', 'что ты такое'),
     lambda: 'Я Ева — твой голосовой ассистент. Скажите «Ева» и команду.'),
    (('что ты умеешь', 'какие команды', 'список команд', 'что можешь',
      'чем можешь помочь', 'помощь', 'help'),
     lambda: ('Могу открывать приложения и сайты, управлять громкостью, '
              'яркостью, вай-фаем и темой, ставить таймер, искать файлы, '
              'делать скриншоты, рассказывать погоду, новости и курсы валют.')),
    (('отбой', 'выключайся', 'иди спать', 'полетели'),
     lambda: 'Хорошо, буду на связи.'),
]


def find_shortcut(text):
    """Возвращает готовый ответ, если команда совпадает с шорткатом,
    иначе None."""
    t = text.lower().strip(' ,.!?…')
    for phrases, fn in SHORTCUTS:
        for p in phrases:
            if t == p or t.startswith(p + ' ') or t.startswith(p + ','):
                return fn()
    return None


# ============================== PIPELINE STEPS ==============================

_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_last_used = 0.0


def get_whisper_model():
    """faster-whisper грузится лениво — только при первой активации,
    чтобы в фоне (в том числе сразу после логина) демон не жрал RAM/CPU."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE,
                    device=WHISPER_DEVICE,
                    compute_type=WHISPER_COMPUTE_TYPE,
                    cpu_threads=WHISPER_CPU_THREADS,
                    num_workers=1,
                )
                _log(f'[jarvis] faster-whisper ({WHISPER_MODEL_SIZE}, int8) загружен')
    return _whisper_model


def _unload_whisper_if_idle():
    """Если команды давно не было — выгружаем faster-whisper из памяти
    (освобождает ~270 МБ ОЗУ; загрузится сам при следующей команде)."""
    global _whisper_model
    if _whisper_model is None or _whisper_last_used == 0:
        return
    if time.monotonic() - _whisper_last_used <= WHISPER_UNLOAD_IDLE_SECONDS:
        return
    _log(f'[jarvis] faster-whisper выгружен (простой > '
         f'{WHISPER_UNLOAD_IDLE_SECONDS // 60} мин)')
    _whisper_model = None


# Модели Ollama в порядке предпочтения: сначала настроенная, затем
# подходящие для слабого железа, затем любые установленные. Выбирается
# первая доступная — так демон работает «из коробки», даже если нужная
# модель ещё не скачана.
OLLAMA_PREFERENCE = [
    OLLAMA_MODEL,
    'qwen2.5:1.5b-instruct',
    'qwen2.5:3b-instruct',
    'gemma2:2b',
    'qwen2.5:7b-instruct',
]

_active_ollama_model = None

# Сколько держать локальную модель загруженной после последнего запроса
# (дольше — быстрее ответ после паузы, но больше занятой ОЗУ).
OLLAMA_KEEP_ALIVE = '30m'

# Через столько секунд простоя снова прогоняем пустышку, чтобы модель
# не успела выгрузиться из памяти (keep_alive) и первый вопрос после
# паузы не ждал 60-70 с обработки промпта.
OLLAMA_REWARM_IDLE_SECONDS = 20 * 60

_ollama_warmup_done = False
_ollama_warmup_lock = threading.Lock()
_ollama_rewarming = threading.Event()
_ollama_last_used = 0.0


def _ollama_touched():
    global _ollama_last_used
    _ollama_last_used = time.monotonic()


def _ollama_warmup_request():
    """Один «пустой» запрос с tools: прогревает кеш префикса модели."""
    if not _active_ollama_model:
        return
    chat = [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': 'привет'}]
    _http.post(OLLAMA_CHAT_URL, json={
        'model': _active_ollama_model,
        'messages': chat,
        'stream': False,
        'tools': TOOLS if ALLOW_SYSTEM_ACTIONS else None,
        'keep_alive': OLLAMA_KEEP_ALIVE,
    }, timeout=180)
    _ollama_touched()


def warmup_ollama():
    """Прогревает локальную модель после старта. Первый запрос с tools
    обрабатывает ~1300 токенов системного промпта на CPU (70+ с), а
    последующие за счёт кеша префикса отвечают за 2-5 с. Чтобы разовая
    пауза не попала на первый вопрос пользователя — гоняем пустышку
    в фоне сразу после готовности демона."""
    global _ollama_warmup_done
    if _ollama_warmup_done:
        return
    with _ollama_warmup_lock:
        if _ollama_warmup_done:
            return
        _ollama_warmup_done = True
    _ollama_rewarming.set()
    try:
        time.sleep(5)  # не конкурируем с логином и стартом демона
        _ollama_warmup_request()
        _log('[jarvis] локальная модель прогрета — первые ответы быстрые')
    except Exception as e:
        _log(f'[jarvis] прогрев локальной модели не удался: {e}')
    finally:
        _ollama_rewarming.clear()


def _maybe_rewarm_ollama():
    """Если локальная модель давно не использовалась — перегреваем её
    в фоне, чтобы первый вопрос после паузы отвечал быстро."""
    if _ollama_last_used == 0 or _ollama_rewarming.is_set():
        return
    if not _active_ollama_model:
        return
    if time.monotonic() - _ollama_last_used < OLLAMA_REWARM_IDLE_SECONDS:
        return
    _ollama_rewarming.set()

    def job():
        try:
            _ollama_warmup_request()
            _log('[jarvis] локальная модель перегрета после простоя')
        except Exception as e:
            _log(f'[jarvis] перегрев модели не удался: {e}')
        finally:
            _ollama_rewarming.clear()

    threading.Thread(target=job, daemon=True).start()


def pick_ollama_model():
    """Определяет на старте, какая модель реально скачана в Ollama."""
    global _active_ollama_model
    try:
        resp = _http.get('http://localhost:11434/api/tags', timeout=5)
        resp.raise_for_status()
        installed = [m.get('name', '') for m in resp.json().get('models', [])]
    except requests.RequestException as e:
        _log(f'[jarvis] Ollama недоступна: {e}')
        _active_ollama_model = OLLAMA_MODEL
        return _active_ollama_model

    for candidate in OLLAMA_PREFERENCE:
        for name in installed:
            # Точное совпадение, либо совпадение по конкретной версии
            # (например, qwen2.5:7b-instruct → любая qwen2.5:7b*), но НЕ
            # по всему семейству (qwen2.5:1.5b не должна подходить под
            # любую qwen2.5).
            family = ':'.join(candidate.split(':')[:2])
            if name == candidate or name.startswith(family + ':'):
                _active_ollama_model = name
                if name != OLLAMA_MODEL:
                    _log(f'[jarvis] модель {OLLAMA_MODEL} не найдена — '
                         f'использую установленную {name}')
                return name

    if installed:
        # Ни одна предпочтительная не найдена — берём любую установленную,
        # чтобы демон не падал с 404 на первом же вопросе.
        name = installed[0]
        _log('[jarvis] ни одна из предпочтительных моделей не найдена — '
             f'использую установленную {name}')
        _active_ollama_model = name
        return name

    _log('[jarvis] в Ollama не найдено ни одной подходящей модели — '
         f'использую {OLLAMA_MODEL}. Выполните: ollama pull qwen2.5:1.5b-instruct')
    _active_ollama_model = OLLAMA_MODEL
    return _active_ollama_model


def record_command(initial_frames=None):
    """Пишет аудио, пока не закончится тишина, возвращает путь к wav-файлу.

    initial_frames — «хвост» аудио, записанный ещё ДО активации (команда,
    начатая в том же вдохе, что и «Ева», сохраняется).
    """
    frames = list(initial_frames) if initial_frames else []
    silence_seconds = 0.0
    total_seconds = len(frames) * (BLOCK_SIZE / SAMPLE_RATE)
    block_seconds = BLOCK_SIZE / SAMPLE_RATE

    while True:
        try:
            chunk = audio_queue.get(timeout=SILENCE_HANG_SECONDS + 1)
        except queue.Empty:
            break

        frames.append(chunk)
        total_seconds += block_seconds

        if is_silence(rms(chunk)):
            silence_seconds += block_seconds
        else:
            silence_seconds = 0.0

        if silence_seconds >= SILENCE_HANG_SECONDS or total_seconds >= MAX_COMMAND_SECONDS:
            break

    pcm = b''.join(frames)
    tmp_path = os.path.join(tempfile.gettempdir(), 'jarvis_command.wav')
    save_wav(tmp_path, pcm)
    return tmp_path


def transcribe(wav_path, whisper_model):
    global _whisper_last_used
    _whisper_last_used = time.monotonic()
    segments, _info = whisper_model.transcribe(
        wav_path,
        language='ru',
        beam_size=WHISPER_BEAM_SIZE,
        vad_filter=True,               # пропускает тишину — быстрее и меньше «галлюцинаций»
        # Но VAD по умолчанию срезает тихое начало/конец фразы (400 мс
        # запаса) — для коротких команд на слабом микрофоне это теряет
        # первые слова. Добавляем запас 600 мс и не требуем длинной речи.
        vad_parameters={
            # Порог 0.35 вместо 0.5 по умолчанию: шёпот (даже после
            # усиления микрофона) не должен вырезаться VAD как «не-речь».
            'threshold': 0.35,
            'min_silence_duration_ms': 800,
            'speech_pad_ms': 600,
            'min_speech_duration_ms': 150,
        },
        condition_on_previous_text=False,  # короткие команды — без повторов-зацикливаний
        initial_prompt=WHISPER_INITIAL_PROMPT,  # правильное написание частых слов
    )
    text = ' '.join(seg.text.strip() for seg in segments).strip()
    return text


def _ollama_generate_fallback(user_text, chat_messages):
    """Запасной путь без function calling — на случай, если модель/Ollama
    не поддерживает tools (например, старая версия Ollama)."""
    context = ''
    for m in chat_messages:
        if m.get('role') == 'user':
            context += f"Пользователь: {m['content']}\n"
        elif m.get('role') == 'assistant' and m.get('content'):
            context += f"Ева: {m['content']}\n"
    prompt = f'{context}Пользователь: {user_text}\nЕва:'
    payload = {
        'model': _active_ollama_model or OLLAMA_MODEL,
        'prompt': prompt,
        'system': SYSTEM_PROMPT,
        'stream': False,
        'keep_alive': OLLAMA_KEEP_ALIVE,
    }
    resp = _http.post(OLLAMA_URL, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json().get('response', '').strip()


# ============================== ОБЛАКО: 429-РЕЖИМ ==========================
# Счётчик сбоев и «остывание» бесплатного облака: после нескольких 429/ошибок
# подряд облако на время отключается (см. _cloud_ok): в такие минуты
# Ева отвечает текстом (уже выполнившей действия) локальной модели.

_cloud_state = {'faults': 0, 'cooldown_until': 0}
_cloud_lock = threading.Lock()


def _cloud_fault():
    """Регистрирует сбой облака. Возвращает True, если набралось
    CLOUD_MAX_FAULTS подряд — облако уходит в остывание."""
    with _cloud_lock:
        _cloud_state['faults'] += 1
        if (_cloud_state['faults'] >= CLOUD_MAX_FAULTS
                and time.monotonic() >= _cloud_state['cooldown_until']):
            _cloud_state['cooldown_until'] = (time.monotonic()
                                              + CLOUD_COOLDOWN_SEC)
            _log(f'[cloud] {CLOUD_MAX_FAULTS} сбоя подряд — облако в '
                 f'остывании {CLOUD_COOLDOWN_SEC} с, отвечаю локальной '
                 f'моделью')
            return True
        return False


def _cloud_ok():
    """True, если облако можно использовать (включено и не в остывании)."""
    if not CLOUD_ENABLED:
        return False
    return time.monotonic() >= _cloud_state['cooldown_until']


def _cloud_success():
    """Успешный ответ облака сбрасывает счётчик сбоев."""
    with _cloud_lock:
        _cloud_state['faults'] = 0
        _cloud_state['cooldown_until'] = 0


def _cloud_429_wait(status_code):
    """При 429 закрываемся на CLOUD_RETRY_DELAY_SEC и пробуем ещё раз
    (решение принято в ask_openai*)."""
    time.sleep(CLOUD_RETRY_DELAY_SEC)


def _compose_cloud_prompt(user_text, local_outcomes):
    """Промпт для облака. Результаты действий локальной модели кладём
    в промпт служебной запиской — так облако учтёт выполненное, но
    не будет повторять инструменты (в облачный запрос tools НЕ идут:
    их роль исполняла локальная модель)."""
    if not local_outcomes:
        return user_text
    return (f'{user_text}\n\n'
            '[Служебная информация: локальная модель уже выполнила '
            'действия:\n'
            f'{local_outcomes}\n'
            'Опирайся на это, не вызывай действия повторно.]')


def _may_need_system_action(text):
    """Быстрый фильтр: может ли запрос требовать системных действий.
    Маркеры соответствуют описаниям инструментов (SYSTEM_ACTION_TRIGGERS).
    Если ни одного слова не найдено — локальная модель не вызывается
    вообще, ответ сразу формирует облако (экономия ~1.5-3 с)."""
    t = text.lower()
    return any(k in t for k in SYSTEM_ACTION_TRIGGERS)


def execute_local_tools(user_text, chat_messages):
    """Локальная модель — «рабочая»: выполняет инструменты (открыть
    приложение, время, таймер, громкость...), но вслух НЕ отвечает.
    Итоговый ответ озвучивает облако (см. ask_llm).

    Возвращает (text, outcomes):
      text     — итоговый текст локальной модели (после вызова
                 инструментов — короткое подтверждение; используется
                 как запасной ответ, если облако недоступно);
      outcomes — «имя_функции: результат», уходит облаку как контекст.

    chat_messages не изменяет."""
    tools = TOOLS if ALLOW_SYSTEM_ACTIONS else None
    msgs = list(chat_messages)
    msgs.append({'role': 'user', 'content': user_text})
    outcomes = []
    text = ''

    for _round in range(MAX_TOOL_ROUNDS):
        payload = {
            'model': _active_ollama_model or OLLAMA_MODEL,
            'messages': msgs,
            'stream': False,
            'keep_alive': OLLAMA_KEEP_ALIVE,
        }
        if tools:
            payload['tools'] = tools

        try:
            resp = _http.post(OLLAMA_CHAT_URL, json=payload, timeout=120)
            resp.raise_for_status()
        except requests.RequestException as e:
            _err(f'[ollama] ошибка запроса (рабочая модель): {e}')
            try:
                text = _ollama_generate_fallback(user_text, msgs[:-1])
            except requests.RequestException as e2:
                _err(f'[ollama] ошибка запроса (fallback): {e2}')
            break
        _ollama_touched()

        data = resp.json() or {}
        message = data.get('message') or {}
        tool_calls = message.get('tool_calls') or []

        if not tool_calls:
            text = (message.get('content') or '').strip()
            break

        # модель просит выполнить действия — делаем и отдаём результаты обратно
        msgs.append(message)
        for call in tool_calls:
            fn_info = call.get('function') or {}
            fname = fn_info.get('name')
            fargs = fn_info.get('arguments') or {}
            if isinstance(fargs, str):
                try:
                    fargs = json.loads(fargs)
                except json.JSONDecodeError:
                    fargs = {}
            _log(f'[jarvis] локальная модель выполняет: {fname}({fargs})')
            result = execute_tool(fname, fargs)
            _log(f'[jarvis] результат: {result}')
            outcomes.append(f'{fname}: {result}')
            msgs.append({'role': 'tool', 'name': fname, 'content': result})

    if text and outcomes:
        # локальная модель коротко подтвердила выполнение — тоже контекст
        outcomes.append(f'локальная модель: {text}')
    return text, '; '.join(outcomes)


def _local_fallback_answer(user_text, chat_messages, local_text):
    """Запасной путь: облако недоступно — отдаём текст локальной модели
    (действия она к этому моменту уже выполнила). История пополняется
    только этим реально сказанным ответом."""
    answer = (local_text or '').strip()
    if not answer:
        return ('Извините, не удалось получить ответ ни от облачной '
                'модели, ни от локальной.')
    chat_messages.append({'role': 'user', 'content': user_text})
    chat_messages.append({'role': 'assistant', 'content': answer})
    return answer


def ask_openai(user_text, chat_messages, with_tools=False):
    """Облачный бэкенд — «оратор»: любой OpenAI-совместимый
    /chat/completions (бесплатный opencode.ai/zen, OpenAI, OpenRouter,
    Groq и т.п.). Инструменты по умолчанию НЕ передаются: действия уже
    выполнила локальная модель (см. execute_local_tools), облако только
    формулирует итоговый ответ. with_tools=True — на случай, если
    облако понадобится как исполнитель.

    chat_messages — общая история диалога, изменяется на месте.
    При ошибке сети/сервера поднимает исключение (ask_llm переключится
    на текст локальной модели)."""
    chat_messages.append({'role': 'user', 'content': user_text})

    url = OPENAI_BASE_URL.rstrip('/') + '/chat/completions'
    headers = {}
    if OPENAI_API_KEY:
        headers['Authorization'] = f'Bearer {OPENAI_API_KEY}'
    tools = TOOLS if (ALLOW_SYSTEM_ACTIONS and with_tools) else None

    for _round in range(MAX_TOOL_ROUNDS):
        payload = {
            'model': OPENAI_MODEL,
            'messages': chat_messages,
            'stream': False,
        }
        if tools:
            payload['tools'] = tools

        # При 429/5xx и таймаутах (бесплатные эндпоинты перегружаются
        # каждые несколько секунд) — пауза и повторные попытки; серия
        # сбоев подряд уводит облако в «остывание» (см. _cloud_ok),
        # после чего отвечает локальная модель.
        for _attempt in range(2):
            try:
                resp = _http.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code in (429, 500, 502, 503, 504):
                    _cloud_429_wait(resp.status_code)
                    continue
                resp.raise_for_status()
                break
            except requests.Timeout:
                _warn(f'[cloud] таймаут (попытка {_attempt + 1}) — повторяю')
                time.sleep(CLOUD_RETRY_DELAY_SEC)
                continue
            except requests.RequestException as e:
                _cloud_fault()
                _err(f'[cloud] ошибка запроса: {e}')
                raise RuntimeError(f'облако недоступно: {e}')
        else:
            _cloud_fault()
            _err('[cloud] все попытки исчерпаны (перегрузка)')
            raise RuntimeError('облако перегружено — использую локальную модель')
        _cloud_success()

        message = (resp.json().get('choices') or [{}])[0].get('message', {}) or {}
        tool_calls = message.get('tool_calls') or []

        if not tool_calls:
            content = (message.get('content') or '').strip()
            chat_messages.append({'role': 'assistant', 'content': content})
            return content or 'Готово.'

        # модель просит вызвать функции — выполняем и отправляем результаты
        # (у OpenAI каждый результат привязывается через tool_call_id)
        chat_messages.append(message)
        for call in tool_calls:
            fn_info = call.get('function', {}) or {}
            fname = fn_info.get('name')
            fargs = fn_info.get('arguments') or {}
            if isinstance(fargs, str):
                try:
                    fargs = json.loads(fargs)
                except json.JSONDecodeError:
                    fargs = {}
            _log(f'[jarvis] вызов действия: {fname}({fargs})')
            result = execute_tool(fname, fargs)
            _log(f'[jarvis] результат: {result}')
            chat_messages.append({
                'role': 'tool',
                'tool_call_id': call.get('id', ''),
                'name': fname,
                'content': result,
            })

    return 'Извините, не получилось довести действие до конца.'


def ask_llm(user_text, chat_messages):
    """БЕЗ гонки бэкендов: локальная модель РАБОТАЕТ, облако ОТВЕЧАЕТ.

    Строго последовательно:
      1) локальная Ollama выполняет действия (инструменты) и сама
         НЕ озвучивается — её результат идёт облаку как контекст;
      2) облако формулирует итоговый ответ, который и читается вслух;
      3) запасной путь: облако недоступно (остывание 429/5xx, нет
         сети) — озвучивается текст локальной модели, чтобы ассистент
         не молчал совсем.

    chat_messages — общая история диалога, изменяется на месте
    (пополняется только реально сказанным ответом облака)."""
    # 1) локальная модель «работает»
    local_text, local_outcomes = execute_local_tools(user_text, chat_messages)

    # 2) облако отвечает
    if not _cloud_ok():
        _warn('[cloud] облако отключено (остывание) — '
              'запасной ответ текстом рабочей модели')
        return _local_fallback_answer(user_text, chat_messages, local_text)

    try:
        msgs = list(chat_messages)  # копия: при сбое облака историю не портим
        answer = ask_openai(
            _compose_cloud_prompt(user_text, local_outcomes), msgs)
    except Exception as e:
        _err(f'[cloud] облачный ответ не удался: {e}')
        return _local_fallback_answer(user_text, chat_messages, local_text)

    chat_messages[:] = msgs
    return answer


# ============================== СТРИМ-ОТВЕТ И ОЗВУЧКА ======================
#
# Обычный путь (ask_llm) ждёт ПОЛНЫЙ ответ модели и только потом начинает
# говорить. Облако при этом генерирует 5-20 с, и всё это время Ева молчит.
# Стрим-путь (ask_llm_streaming) убирает паузу: сначала локальная модель
# выполняет действия, затем облачный запрос уходит с stream=True,
# и как только пришёл первый осмысленный фрагмент — текст нарезается на
# предложения и сразу озвучивается локальным голосом RHVoice (мгновенный
# синтез, без сети). Речь идёт ПАРАЛЛЕЛЬНО с генерацией облака.

_TOOL_FALLBACK = '\x00jarvis_tool_fallback\x00'


def _sentence_key(s):
    """Нормализованный «отпечаток» предложения для сравнения дублей."""
    k = re.sub(r'[^\w\d\s]', '', s.lower())
    return re.sub(r'\s+', ' ', k).strip()


def _is_dup(key, seen):
    """Дубль ли предложение (key) среди уже сказанных (seen). Модели
    часто повторяют один и тот же смысл по 3-4 раза подряд — такие
    повторы не должны читаться вслух."""
    for prev in seen:
        if key == prev:
            return True
        if len(key) >= 10 and len(prev) >= 10 and (key in prev or prev in key):
            return True
        if len(key) >= 4 and len(prev) >= 4:
            try:
                if difflib.SequenceMatcher(None, key, prev).ratio() >= 0.88:
                    return True
            except Exception:
                pass
    return False


def _dedup_sentences(text):
    """Выкидывает из текста дубли и почти-дубли предложений (для обычного
    пути озвучки, когда ответ читается целиком одним куском)."""
    parts = re.split(r'(?<=[.!?…])(?:\s+|(?=[А-ЯЁA-Z"«]))', text)
    out = []
    seen = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        key = _sentence_key(p)
        if key and _is_dup(key, seen):
            continue
        if key:
            seen.append(key)
            if len(seen) > 8:
                seen.pop(0)
        out.append(p)
    return ' '.join(out)


class StreamSpeak:
    """Нарезает потоковый текст на предложения и отдаёт их наружу по мере
    появления: короткая фраза озвучивается сразу, длинный кусок без точек
    — принудительно режется, чтобы не копить молчание."""

    # Граница предложения: знак [.!?…], после которого пробел/перенос ЛИБО
    # сразу буква (русские модели часто пишут без пробела: «четвёрке.Результат»)
    _BOUNDARY = re.compile(r'(?<=[.!?…])(?:\s+|(?=[А-ЯЁA-Z"«]))')
    _FORCED_LIMIT = 140

    def __init__(self, emit):
        self._buf = ''
        self._emit = emit
        self._seen = []  # сказанные предложения — для отсеивания дублей

    def add(self, piece):
        self._buf += piece
        while True:
            m = self._BOUNDARY.search(self._buf)
            if not m:
                break
            end = m.end()
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if sentence:
                self._emit_dedup(sentence)
        if len(self._buf) >= self._FORCED_LIMIT:
            self._emit_dedup(self._buf.strip())
            self._buf = ''

    def flush(self):
        s = self._buf.strip()
        if s:
            self._emit_dedup(s)
        self._buf = ''

    def _emit_dedup(self, sentence):
        """Озвучивает предложение, пропуская дубли/почти-дубли (модель
        часто повторяет один смысл несколько раз подряд)."""
        key = _sentence_key(sentence)
        if key and _is_dup(key, self._seen):
            return
        if key:
            self._seen.append(key)
            if len(self._seen) > 8:
                self._seen.pop(0)
        self._emit(sentence)


# Очередь предложений для голосового воркера (стрим-озвучка).
_voice_queue = queue.Queue()
_voice_worker_started = False


def _voice_worker():
    """Проговаривает предложения по мере поступления СРАЗУ, как только
    модель их выдала (не дожидаясь полного ответа). Прерывание словом
    «Ева» останавливает остаток очереди. Микрофон глушится на всю
    сессию озвучки (защита от эха), затем возвращается в прежнее
    состояние."""
    mic_muted = False
    while True:
        try:
            text = _voice_queue.get(timeout=2.0)
        except queue.Empty:
            if mic_muted:
                _mic_mute_off()
                mic_muted = False
            continue
        if text is None:
            break
        if not mic_muted:
            _mic_mute_on()
            mic_muted = True
        if interrupt_event.is_set():
            continue
        try:
            with _speak_lock:
                if not _speak_rhvoice(text):
                    _speak_piper(text)
        except Exception as e:
            _err(f'[voice] ошибка стрим-озвучки: {e}')
        # микро-пауза между предложениями, чтобы речь звучала связно
        time.sleep(0.15)
    if mic_muted:
        _mic_mute_off()


def start_voice_worker():
    global _voice_worker_started
    if _voice_worker_started:
        return
    _voice_worker_started = True
    threading.Thread(target=_voice_worker, daemon=True).start()


def _clear_voice_queue():
    """Выбрасывает не озвученные предложения (после отката на обычный
    путь ответа), чтобы не было «каши» из старого и нового текста."""
    while True:
        try:
            _voice_queue.get_nowait()
        except queue.Empty:
            break


def ask_openai_stream(user_text, msgs, claim, cancel_check, say,
                      with_tools=False):
    """Облако (OpenAI-совместимый API, stream=True) — «оратор»: текст
    озвучивается по предложениям по мере генерации. Инструменты по
    умолчанию не передаются (действия уже выполнила локальная модель).
    with_tools=True — если облако всё же нужно как исполнитель; тогда
    при уходе модели в вызов инструментов возвращается _TOOL_FALLBACK
    (запрос повторяется обычным путём со стабильной обработкой tools)."""
    msgs2 = list(msgs)
    msgs2.append({'role': 'user', 'content': user_text})

    url = OPENAI_BASE_URL.rstrip('/') + '/chat/completions'
    headers = {}
    if OPENAI_API_KEY:
        headers['Authorization'] = f'Bearer {OPENAI_API_KEY}'
    tools = TOOLS if (ALLOW_SYSTEM_ACTIONS and with_tools) else None

    payload = {
        'model': OPENAI_MODEL,
        'messages': msgs2,
        'stream': True,
    }
    if tools:
        payload['tools'] = tools

    # То же поведение, что и в ask_openai: 429/5xx — пауза и повторные
    # попытки (не больше двух), далее облако уходит в «остывание», и
    # следующие запросы обслуживает локальная модель.
    for _attempt in range(2):
        try:
            resp = _http.post(url, headers=headers, json=payload, timeout=60,
                              stream=True)
        except requests.RequestException as e:
            _cloud_fault()
            _err(f'[cloud] стрим-запрос не прошёл: {e}')
            time.sleep(CLOUD_RETRY_DELAY_SEC)
            continue
        if resp.status_code not in (429, 500, 502, 503, 504):
            break
        try:
            resp.close()
        except Exception:
            pass
        _cloud_429_wait(resp.status_code)
    else:
        _cloud_fault()
        raise RuntimeError('облако перегружено — использую локальную модель')
    resp.raise_for_status()
    _cloud_success()

    claimed = False
    spk = None
    acc = ''
    tool_deltas = {}
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if cancel_check():
                return ('', None, False)
            if not line or not line.startswith('data:'):
                continue
            data = line[len('data:'):].strip()
            if not data or data == '[DONE]':
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            choice = (obj.get('choices') or [{}])[0]
            if not isinstance(choice, dict):
                continue
            delta = choice.get('delta') or {}
            if not isinstance(delta, dict):
                continue
            if delta.get('tool_calls'):
                for t in delta['tool_calls']:
                    idx = t.get('index', 0)
                    entry = tool_deltas.setdefault(idx, {'name': '', 'arguments': ''})
                    fn = t.get('function') or {}
                    if fn.get('name'):
                        entry['name'] += fn['name']
                    if fn.get('arguments'):
                        entry['arguments'] += fn['arguments']
                continue
            content = delta.get('content')
            if content:
                if not claimed:
                    if not claim():
                        return ('', None, False)
                    claimed = True
                if spk is None:
                    spk = StreamSpeak(say)
                spk.add(content)
                acc += content
            if choice.get('finish_reason') == 'stop':
                break
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if tool_deltas:
        return (_TOOL_FALLBACK, None, True)

    if not claimed:
        return ('', None, False)

    spk.flush()
    msgs2.append({'role': 'assistant', 'content': acc})
    return (acc, msgs2, True)


def ask_llm_streaming(user_text, chat_messages, holder=None, max_sentences=None):
    """БЕЗ гонки: локальная модель сначала выполняет действия, затем
    облако стримит итоговый ответ, который озвучивается по предложениям
    по мере генерации (см. ask_llm).

    max_sentences — озвучивать максимум первые N предложений ответа
    (остальное выбрасывается, полный текст всё равно попадает в меню
    расширения); None — без ограничения. На простые вопросы хватает
    одного-двух коротких предложений.

    Возвращает (полный_текст, streamed: bool). Запасной путь при
    недоступном облаке — текст локальной модели (действия она уже
    сделала). holder — необязательный dict: в него пишется flag
    'streamed', как только началась речь (для снятия дедлайна ожидания)."""
    local_text, local_outcomes = '', []
    if _may_need_system_action(user_text):
        local_text, local_outcomes = execute_local_tools(user_text, chat_messages)
    else:
        _log('[jarvis] системных действий не нужно — сразу облако')

    if not _cloud_ok():
        _warn('[cloud] облако отключено (остывание) — '
              'запасной ответ текстом рабочей модели')
        if not local_text:
            try:
                local_text, _ = execute_local_tools(user_text, chat_messages)
            except Exception:
                pass
        return (_local_fallback_answer(
            user_text, chat_messages, local_text), False)

    state = {'progress': '', 'spoken': 0}
    lock = threading.Lock()

    def say(sentence):
        with lock:
            if max_sentences is not None and state['spoken'] >= max_sentences:
                return  # лишние предложения вслух не читаем
            state['spoken'] += 1
            state['progress'] += sentence
            progress = state['progress']
        if holder is not None:
            holder['streamed'] = True
        _voice_queue.put(sentence)
        # «живой» текст в меню расширения по мере генерации
        emit_response(progress + '…')
        emit_state('speaking')

    try:
        answer, msgs, streamed = ask_openai_stream(
            _compose_cloud_prompt(user_text, local_outcomes),
            list(chat_messages),
            claim=lambda: True,
            cancel_check=lambda: service.stop_event.is_set(),
            say=say)
    except Exception as e:
        _err(f'[cloud] стрим-ответ не удался: {e}')
        if not local_text:
            try:
                local_text, _ = execute_local_tools(user_text, chat_messages)
            except Exception:
                pass
        return (_local_fallback_answer(
            user_text, chat_messages, local_text), False)

    if answer == _TOOL_FALLBACK:
        return (_TOOL_FALLBACK, True)

    if not streamed or msgs is None:
        # Облако ничего не озвучило (обрыв стрима) — запасной путь: читаем
        # то, что успела выполнить локальная модель.
        _warn('[cloud] стрим пустой — запасной ответ рабочей модели')
        if not local_text:
            try:
                local_text, _ = execute_local_tools(user_text, chat_messages)
            except Exception:
                pass
        return (_local_fallback_answer(
            user_text, chat_messages, local_text), False)

    chat_messages[:] = msgs
    return (answer, streamed)


def _which(cmd):
    return shutil.which(cmd) is not None


def _piper_bin():
    """Piper ставится pip-ом в тот же venv, что и демон, поэтому его бинарь
    лежит рядом с интерпретатором, но НЕ попадает в PATH systemd-сервиса."""
    on_path = shutil.which('piper')
    if on_path:
        return on_path
    in_venv = os.path.join(os.path.dirname(sys.executable), 'piper')
    if os.path.exists(in_venv):
        return in_venv
    return None


# Озвучка может вызываться из разных потоков (стрим-озвучка + основной
# поток): сериализуем, чтобы не было каши из двух голосов и конфликтов
# за временные файлы.
_speak_lock = threading.Lock()


_CODE_BLOCK_RE = re.compile(r'```.*?```', re.S)


def _sanitize_for_speech(text):
    """Готовит текст для синтеза: убирает эмодзи, маркдаун-мусор и
    слово-активатор «Ева» (если оно попадёт в ответ, ассистент прервёт
    собственную озвучку из-за эха микрофона). Код вслух не читаем — это
    долго и бесполезно на слух, полный текст всё равно виден в меню
    расширения. Слишком длинные ответы озвучиваем сокращённо."""
    if _CODE_BLOCK_RE.search(text):
        text = _CODE_BLOCK_RE.sub(' Код готов, весь текст — в меню ассистента. ', text)
    text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+', '', text)
    text = re.sub(r'[*_#`~|>]', '', text)
    text = re.sub(r'([!?.,]){2,}', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'\b(?:ева|эва)\b', 'я', text, flags=re.I)
    text = text.strip()
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]
        # обрезаем по границе предложения, чтобы не рвать слово на середине
        last_stop = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
        if last_stop > MAX_SPOKEN_CHARS * 0.5:
            cut = cut[:last_stop + 1]
        text = cut + ' Полный ответ — в меню ассистента.'
    return text


# ============================ ЧЕЛОВЕЧЕСКИЙ ОТВЕТ ============================
# Простые вопросы («который час», «сколько будет 2+2») не должны выливаться
# в простыню и дубли: модель часто повторяет одну мысль несколько раз и
# пишет списки/разметку. Приводим ответ к виду живой речи — максимум два
# коротких предложения без маркдауна и повторов.

_MAX_BRIEF_CHARS = 180  # жёсткий предел «короткого» ответа на простой вопрос
_SENT_SPLIT = re.compile(r'(?<=[.!?…])(?:\s+|(?=[А-ЯЁA-Z"«]))')


def _strip_markdown(text):
    """Убирает маркдаун-мусор и пункты списков — оставляет обычный текст."""
    if _CODE_BLOCK_RE.search(text):
        text = _CODE_BLOCK_RE.sub(' Код готов, весь текст — в меню ассистента. ', text)
    text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+', '', text)
    text = re.sub(r'[*_#`~>]', '', text)
    text = re.sub(r'([!?.,]){2,}', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.M)
    # пункты вида «1. текст» / «2) текст» — тоже в живой текст
    # (даты «12.08.2026» не трогаем: после числа идёт цифра)
    text = re.sub(r'^\s*\d+[.)]\s+(?=[^\d.,])', '', text, flags=re.M)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _humanize(text, complex_):
    """Приводит ответ LLM к виду живой устной речи. Для простых вопросов
    (complex_ = False) оставляет максимум два коротких предложения — всё
    остальное отрезается. Сложные задачи (код, длинные тексты) не трогаем."""
    if not text or complex_:
        return text
    text = _strip_markdown(text)
    text = _dedup_sentences(text)
    if not text:
        return text
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    text = ' '.join(parts[:2])
    if len(text) >= _MAX_BRIEF_CHARS:
        cut = text[:_MAX_BRIEF_CHARS]
        # обрезаем по границе запятой/слова, чтобы не рвать середину
        # предложения
        last_words = max(cut.rfind(', '), cut.rfind(' — '), cut.rfind(' - '),
                         cut.rfind(' '))
        if last_words > _MAX_BRIEF_CHARS * 0.4:
            cut = cut[:last_words]
        text = cut.rstrip(' ,-—') + '.'
    return text


# ------------------------------ ЗАЩИТА ОТ ЭХА ------------------------------
# Пока Ева говорит, микрофон глушится через pactl (PipeWire/PulseAudio):
# собственный ответ из динамиков не будит её повторным «Ева» и не мусорит
# в распознавание. Если пользователь сам выключил микрофон — после озвучки
# он останется выключенным.

_mic_ctl_ok = None       # None = ещё не проверено, False = pactl/источника нет
_mic_was_muted = None    # состояние микрофона до нашего глушения


def _mic_mute_on():
    """Глушит вход микрофона. Запоминает, был ли он выключен до этого."""
    global _mic_ctl_ok, _mic_was_muted
    if _mic_ctl_ok is None:
        try:
            probe = subprocess.run(
                ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
                capture_output=True, timeout=5, text=True)
            _mic_ctl_ok = probe.returncode == 0 and 'Mute:' in probe.stdout
        except Exception:
            _mic_ctl_ok = False
        if not _mic_ctl_ok:
            return
    _mic_was_muted = 'yes' in _mic_probe_source()
    try:
        subprocess.run(
            ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '1'],
            capture_output=True, timeout=5)
    except Exception:
        _mic_ctl_ok = False


def _mic_mute_off():
    """Возвращает микрофон в прежнее состояние (не включаем, если
    пользователь сам его отключил)."""
    global _mic_ctl_ok, _mic_was_muted
    if _mic_was_muted is None or not _mic_ctl_ok:
        return
    try:
        if not _mic_was_muted:
            subprocess.run(
                ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '0'],
                capture_output=True, timeout=5)
    except Exception:
        _mic_ctl_ok = False
    finally:
        _mic_was_muted = None


def _mic_probe_source():
    try:
        r = subprocess.run(
            ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
            capture_output=True, timeout=5, text=True)
        return r.stdout or ''
    except Exception:
        return ''


def speak(text):
    """Синтезирует речь и проигрывает. Сначала пробует нейронный голос
    Microsoft (edge-tts, «Светлана» — самый естественный, нужен интернет),
    затем офлайн RHVoice (Elena), и в самом конце — Piper (irina)."""
    if not text:
        return
    # Озвучку прервали повторным словом «Ева» (interrupt_event): новую речь
    # не начинаем и на запасные голоса не переключаемся.
    if interrupt_event.is_set():
        return
    text = _sanitize_for_speech(text)
    # модели часто повторяют один смысл по несколько раз — вслух читаем
    # без дублей
    text = _dedup_sentences(text)
    if not text:
        return
    with _speak_lock:
        # защита от эха: на время речи микрофон заглушён
        _mic_mute_on()
        try:
            if _speak_edge(text):
                return
            if _speak_rhvoice(text):
                return
            _speak_piper(text)
        finally:
            _mic_mute_off()


# ============================== EDGE-TTS ==================================
# Нейронный онлайн-голос Microsoft (бесплатно, без API-ключа). Звучит
# заметно естественнее RHVoice/Piper. Нужен интернет; при сбое демон
# автоматически переходит на офлайн-голоса ниже.

def _stream_edge_to_ffplay(text):
    """edge-tts ПОТОКОМ: первый аудио-чанк сразу уходит в ffplay через
    stdin, не дожидаясь синтеза всей фразы. Для коротких ответов речь
    начинается на 1-2.5 с раньше, чем при записи в файл целиком.
    Возвращает True, если удалось сгенерировать и проиграть."""
    global _player_proc
    try:
        proc = subprocess.Popen(
            ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet',
             '-fflags', 'nobuffer', '-flags', 'low_delay', '-i', 'pipe:0'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    _player_proc = proc
    heard_audio = False
    try:
        import edge_tts

        async def _synth():
            nonlocal heard_audio
            comm = edge_tts.Communicate(
                text, EDGE_TTS_VOICE, rate=EDGE_TTS_RATE)
            async for chunk in comm.stream():
                if chunk['type'] != 'audio':
                    continue
                heard_audio = True
                proc.stdin.write(chunk['data'])
                proc.stdin.flush()

        asyncio.run(_synth())
    except Exception as e:
        _err(f'[tts] edge-tts поток: {e}')
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        _player_proc = None
    if not heard_audio:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return False
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
    return proc.returncode == 0


def _speak_edge(text):
    """Синтезирует речь через edge-tts (web socket Microsoft Edge).
    Возвращает True, если удалось сгенерировать и проиграть.

    Если в системе есть ffplay — ответ озвучивается потоком (речь
    стартует сразу с первым chunk'ом аудио), иначе — файл целиком,
    затем проигрывание."""
    if _which('ffplay') and _stream_edge_to_ffplay(text):
        return True
    try:
        import edge_tts

        audio_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.mp3')

        async def _synth():
            await edge_tts.Communicate(
                text, EDGE_TTS_VOICE, rate=EDGE_TTS_RATE,
            ).save(audio_path)

        asyncio.run(_synth())
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            return False
        return _play_wav(audio_path)
    except Exception as e:
        _warn(f'[tts] edge-tts недоступен ({e}) — использую офлайн-голос')
        return False


# ============================== RHVOICE ===================================

_rhvoice = None
_rhvoice_lock = threading.Lock()


def _patch_rhvoice_bindings():
    """rhvoice-wrapper 0.8.0 ломается на Python 3.14: он передаёт bytes
    в ctypes.CDLL, а 3.14 принимает только str. Чиним его загрузчик."""
    try:
        import rhvoice_wrapper.rhvoice_bindings as rb

        def _selector(lib_path):
            if lib_path is None:
                return 'libRHVoice.so'
            return lib_path if isinstance(lib_path, str) else lib_path.decode()

        rb._lib_selector = _selector
        rb.load_tts_library.__globals__['_lib_selector'] = _selector
        return True
    except Exception:
        return False


def _init_rhvoice():
    """Инициализирует RHVoice (rhvoice-wrapper поверх libRHVoice + голоса).
    Возвращает False, если RHVoice недоступен — тогда используется Piper."""
    global _rhvoice
    if _rhvoice is not None:
        return _rhvoice
    with _rhvoice_lock:
        if _rhvoice is not None:
            return _rhvoice
        try:
            if not _patch_rhvoice_bindings():
                raise RuntimeError('не удалось пропатчить rhvoice_bindings')
            from rhvoice_wrapper import TTS
            kwargs = {'threads': 1}
            if os.path.isdir(RHVOICE_DATA_PATH):
                kwargs['data_path'] = RHVOICE_DATA_PATH
            tts = TTS(**kwargs)
            voices = list(tts.voices)
            if not voices:
                raise RuntimeError('не найдено ни одного голоса RHVoice')
            want = RHVOICE_VOICE.lower()
            voice = next((v for v in voices if v.lower() == want), None) or voices[0]
            _rhvoice = (tts, voice)
            _log(f'[tts] RHVoice: голос {voice} (естественный русский синтез)')
        except Exception as e:
            _warn(f'[tts] RHVoice недоступен ({e}) — использую Piper (irina). '
                  'Для естественного голоса: sudo pacman -S rhvoice '
                  'rhvoice-language-russian rhvoice-voice-elena')
            _rhvoice = False
    return _rhvoice


def _speak_rhvoice(text):
    pair = _init_rhvoice()
    if not pair:
        return False
    tts, voice = pair
    try:
        data = tts.get(text, voice=voice, format_='wav',
                       sets={'relative_rate': RHVOICE_RATE})
        if not data:
            return False
        wav_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.wav')
        with open(wav_path, 'wb') as f:
            f.write(data)
        return _play_wav(wav_path)
    except Exception as e:
        _err(f'[tts] ошибка RHVoice: {e}')
        return False


# ============================== PIPER =====================================

def _speak_piper(text):
    """Синтезирует речь через Piper CLI (женский голос irina) в wav."""
    piper_bin = _piper_bin()
    if not piper_bin:
        _err('[piper] бинарь "piper" не найден — установите piper-tts '
             '(pip install piper-tts) или перезапустите install.sh')
        return

    wav_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.wav')
    cmd = [
        piper_bin,
        '--model', PIPER_VOICE_MODEL,
        '--output_file', wav_path,
    ]
    if PIPER_LENGTH_SCALE != 1.0:
        cmd += ['--length_scale', str(PIPER_LENGTH_SCALE)]
    try:
        subprocess.run(
            cmd,
            input=text.encode('utf-8'),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        _err(f'[piper] ошибка синтеза: {e.stderr.decode(errors="ignore")}')
        return
    except FileNotFoundError:
        _err(f'[piper] бинарь "{piper_bin}" не найден — проверьте установку piper-tts')
        return

    _play_wav(wav_path)


def _play_wav(wav_path):
    """Проигрывает wav-файл. Порядок плееров: paplay (pulse/pipewire-pulse) →
    pw-play (pipewire) → ffplay (ffmpeg) → aplay (alsa). Возвращает True,
    если удалось воспроизвести. Процесс плеера можно прервать повторным
    словом «Ева» (см. InterruptMonitor)."""
    global _player_proc
    for player in ('paplay', 'pw-play', 'ffplay', 'aplay'):
        if interrupt_event.is_set():
            return False  # прервано словом «Ева» — не играть дальше
        if not _which(player):
            continue
        cmd = [player, wav_path]
        if player == 'ffplay':
            cmd = ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', wav_path]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        _player_proc = proc
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False
        finally:
            _player_proc = None
        if proc.returncode == 0:
            return True
        # плеер не смог (или его убили) — пробуем следующий
        _err(f'[player] {player} не смог воспроизвести ответ '
             f'(код {proc.returncode})')

    _err('[player] ни один плеер не воспроизвёл ответ '
         '(нет paplay/pw-play/ffplay/aplay?)')
    return False


_player_proc = None


def _kill_player():
    """Немедленно останавливает озвучку (вызывается из InterruptMonitor)."""
    if _player_proc is not None and _player_proc.poll() is None:
        try:
            _player_proc.kill()
        except Exception:
            pass


# ============================== СИГНАЛ АКТИВАЦИИ ==============================
# Вместо голосового «Слушаю» при слове-активаторе играем короткий «пик» —
# не перебивает мысль голосом и сразу понятен на слух.

_ATTENTION_WAV = os.path.join(tempfile.gettempdir(), 'jarvis_attention.wav')


def _make_attention_wav(path):
    """Генерирует короткий (0.25 с) двухнотный сигнал с плавным затуханием."""
    sr = 44100
    total = int(sr * 0.25)
    t = np.arange(total) / sr
    n1 = int(sr * 0.11)
    tone = np.concatenate([
        np.sin(2 * np.pi * 880.0 * t[:n1]),      # ля5
        np.sin(2 * np.pi * 1318.5 * t[n1:]),     # ми6
    ])
    # плавный удар и затухание, чтобы не было щелчков
    n_fade = int(sr * 0.02)
    env = np.ones(total)
    env[:n_fade] = np.linspace(0, 1, n_fade, endpoint=False)
    env[-n_fade:] = np.linspace(1, 0, n_fade)
    pcm = (tone * env * 0.6 * 32767).astype('<i2')
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _play_attention_sound():
    """Играет сигнал активации. Если не вышло — запасной вариант голосом."""
    try:
        if not os.path.exists(_ATTENTION_WAV):
            _make_attention_wav(_ATTENTION_WAV)
        if not _play_wav(_ATTENTION_WAV):
            raise RuntimeError('ни один плеер не воспроизвёл сигнал')
    except Exception as e:
        _warn(f'[sound] не удалось проиграть сигнал активации ({e}) — '
              'голосовое «Слушаю»')
        try:
            speak('Слушаю')
        except Exception:
            pass


def _build_wake_pattern():
    """Фонетический шаблон слова-активатора. Из «Ева» получается примерно
    «[йъь]?[еэ]в[аоуы]»: распознанное слово может начинаться с призвука
    «й/ъ/ь», первая гласная «е/э» взаимозаменяема, а последняя гласная —
    любая, потому что маленькая модель пишет «ево», «еву», «эво» и т.п.
    вместо «Ева». При этом «дева», «нева», «лева» (в них «ева» есть лишь
    буквально) шаблону НЕ совпадают и ассистента не активируют."""
    for w in WAKE_WORDS:
        w = w.lower().strip()
        if not w:
            continue
        lead, core, tail = w[0], w[1:-1], w[-1]
        lead_cls = '[еэ]' if lead in 'еэ' else re.escape(lead)
        tail_cls = '[аоуы]' if tail in 'аоуы' else re.escape(tail)
        return re.compile(f'^[йъь]?{lead_cls}{re.escape(core)}{tail_cls}')


# Слова, где «ева/эва» — лишь окончание: на них ассистент просыпаться
# не должен, даже если нечёткий матч посчитает их «похожими».
_WAKE_FALSE_FRIENDS = frozenset((
    'дева', 'нева', 'лева', 'дива', 'лива', 'слива', 'нива', 'тива',
    'тиво', 'вева', 'еве', 'ёва', 'йова', 'жива', 'живе', 'рева',
    'евро', 'евра', 'евре', 'евва',
))
# Порог нечёткого совпадения слова с «Ева» (SequenceMatcher.ratio):
# 0.75 пропускает «йева», «еваа»-подобные огрехи модели, но отсекает
# «ава», «тва», «ява» и явно посторонние слова.
_WAKE_FUZZY_RATIO = 0.75


def _wake_fuzzy_match(token):
    """Нечёткое совпадение слова с одним из вариантов слова-активатора.
    Ловит искажения маленькой Vosk-модели («йова», «вева», «еваа»),
    которые точный шаблон уже не видит. Слово должно НАЧИНАТЬСЯ как
    «Ева» (возможен призвук «й/ъ/ь») — иначе это чужое слово («рева»,
    «дева», «евро»), в котором «ева» лишь буквально присутствует."""
    if len(token) < 3 or len(token) > 5:
        return False
    if token in _WAKE_FALSE_FRIENDS:
        return False
    if not re.match(r'^[йъь]?[еэ]', token):
        return False
    for w in WAKE_WORDS:
        w = w.lower().strip()
        if not w:
            continue
        if difflib.SequenceMatcher(None, token, w).ratio() >= _WAKE_FUZZY_RATIO:
            return True
    return False


def _wake_token_match(token):
    """Длина совпадения начала слова с словом-активатором: точный шаблон
    даёт длину совпадения в начале слова, нечёткий — всё слово целиком.
    Возвращает 0, если слово не похоже на активацию."""
    token = token.lower()
    m = _WAKE_PATTERN.search(token)
    if m:
        return m.end()
    return len(token) if _wake_fuzzy_match(token) else 0
    return re.compile(r'^[йъь]?[еэ][в][аоуы]')


_WAKE_PATTERN = _build_wake_pattern()


def contains_wake_word(text):
    """True, если в распознанном тексте есть слово-активатор — точное
    («ева», «эва»), похожие варианты распознавания («ево», «эво»,
    «еву», «йева») или нечёткие искажения маленькой модели («йова»,
    «вева»). Слова проверяем отдельными токенами, чтобы «дева»,
    «нева», «лева» не активировали ассистента случайно."""
    text = text.lower()
    return any(_wake_token_match(t) > 0 for t in re.findall(r'[а-яё]+', text))


def strip_wake_word(text):
    """Убирает слово-активатор из начала фразы («ева, открой браузер»
    → «открой браузер», «ево открой» → «открой»), чтобы модель не
    получала лишнего слова. Слова «дева», «лева» не трогаем."""
    m = re.match(r'^(?P<pre>[^а-яё]*)(?P<word>[а-яё]+)', text, re.IGNORECASE)
    if not m:
        return text
    match_len = _wake_token_match(m.group('word').lower())
    if not match_len:
        return text
    cut = m.start('word') + match_len
    return text[cut:].lstrip(' ,.…-:')


# Событие прерывания: повторное слово «Ева» во время обработки/озвучки
# останавливает текущий запрос, и демон слушает новую команду.
interrupt_event = threading.Event()


class InterruptMonitor:
    """Следит за микрофоном, пока демон думает или говорит. Если
    пользователь снова произносит слово-активатор — прерывает текущий
    запрос (ставит interrupt_event и останавливает озвучку)."""

    def __init__(self, vosk_model):
        self._rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        interrupt_event.clear()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = audio_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._rec.AcceptWaveform(chunk):
                text = json.loads(self._rec.Result()).get('text', '')
            else:
                text = json.loads(self._rec.PartialResult()).get('partial', '')
            if text and contains_wake_word(text):
                _log('[jarvis] прерывание: снова сказано слово-активатор')
                interrupt_event.set()
                _kill_player()
                return


def _drain_audio_queue():
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break


# ============================== MAIN LOOP ==============================

def worker_loop():
    # На слабом железе даём GNOME Shell спокойно стартовать после логина,
    # иначе демон и оболочка конкурируют за CPU/RAM в самый разгар запуска.
    time.sleep(STARTUP_DELAY_SECONDS)

    try:
        _log('[jarvis] загружаю Vosk-модель...')
        vosk_model = VoskModel(VOSK_MODEL_PATH)
        recognizer = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    except Exception as e:
        _log(f'[jarvis] НЕ удалось загрузить Vosk-модель: {e}')
        _log('[jarvis] Проверьте, что install.sh скачал модель в '
             f'{VOSK_MODEL_PATH} (модель скачивается с alphacephei.com).')
        emit_state('offline')
        return  # D-Bus остаётся жить, расширение покажет «Демон не запущен»

    # faster-whisper сюда НЕ грузим — он появится лениво при первой команде
    # (см. get_whisper_model). А вот модель LLM определяем сразу, чтобы
    # не «упасть» на первом же вопросе из-за нескачанной модели.
    _log('[jarvis] LLM-бэкенды: облако (OpenAI-совместимый API: '
         f'{OPENAI_BASE_URL}, модель {OPENAI_MODEL}) + локальная Ollama')
    if not OPENAI_API_KEY and 'opencode.ai' not in OPENAI_BASE_URL:
        _log('[jarvis] ВНИМАНИЕ: OPENAI_API_KEY не задан — облачный '
             'бэкенд не сможет отвечать. Укажите ключ в конфиге.')
    # Локальная модель — «рабочая» на каждый запрос (выполняет действия),
    # поэтому определяем её сразу, пока облако в режиме ответов.
    pick_ollama_model()

    chat_messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

    try:
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            device=MIC_DEVICE,
            dtype='int16',
            channels=1,
            callback=audio_callback,
        )
    except Exception as e:
        _log(f'[jarvis] НЕ удалось открыть микрофон: {e}')
        _log('[jarvis] Проверьте звук (pactl info) и настройку MIC_DEVICE.')
        emit_state('offline')
        return

    with stream:
        mode_hint = {
            'voice': 'Говорите "Ева" для активации.',
            'hotkey': 'Режим: активация только по горячей клавише.',
            'both': 'Говорите "Ева" или нажмите горячую клавишу.',
        }
        _log(f'[jarvis] готов. {mode_hint.get(ACTIVATION_MODE, "")}')
        emit_state('idle')

        # Голосовой воркер для стрим-озвучки ответов (предложения текста,
        # приходящие от LLM по мере генерации, читаются сразу)
        start_voice_worker()

        # Прогреваем локальную модель в фоне (первый ответ с tools
        # после старта иначе обрабатывается на CPU 60-70 с)
        threading.Thread(target=warmup_ollama, daemon=True).start()

        while not service.stop_event.is_set():
            try:
                if not _wake_listen(service, recognizer):
                    continue
            except Exception as e:
                _log(f'[jarvis] ошибка в цикле прослушивания: {e}')
                continue

            recognizer.Reset()

            # После прерывания («Ева» во время ответа) слово уже сказано —
            # сразу слушаем новую команду, без повторной активации.
            while not service.stop_event.is_set():
                try:
                    status = run_command_flow(vosk_model, chat_messages)
                except Exception as e:
                    _log(f'[jarvis] ошибка в обработке команды: {e}')
                    status = 'done'
                if status != 'interrupted':
                    break
                _log('[jarvis] слушаю новую команду...')
            recognizer.Reset()

            # Режим непрерывного диалога (как у Алисы): после ответа Ева
            # продолжает слушать уточнения БЕЗ слова-активатора, пока
            # пользователь говорит. Каждая реплика продлевает диалог;
            # тишина (DIALOGUE_TIMEOUT_SECONDS) или пауза возвращают
            # в обычное ожидание «Ева».
            if (status == 'done' and DIALOGUE_MODE_ENABLED
                    and service.activation_mode != 'hotkey'):
                _log('[jarvis] диалоговый режим: слушаю уточнения без «Ева»...')
                try:
                    while not service.stop_event.is_set():
                        if not _dialogue_listen(service, recognizer):
                            break
                        recognizer.Reset()
                        try:
                            status = run_command_flow(
                                vosk_model, chat_messages, dialogue=True)
                        except Exception as e:
                            _log(f'[jarvis] ошибка в обработке команды: {e}')
                            status = 'done'
                        recognizer.Reset()
                        if status != 'done':
                            break
                except Exception as e:
                    _log(f'[jarvis] ошибка в диалоговом режиме: {e}')
                _log('[jarvis] возврат к ожиданию «Ева»')


def run_command_flow(vosk_model, chat_messages, dialogue=False):
    """Полный цикл одной команды: запись → распознавание → LLM → озвучка.

    dialogue — True, если это продолжение разговора в диалоговом режиме
    (без слова-активатора): сигнал активации тогда не играется.

    Возвращает:
      'done'        — команда обработана;
      'empty'       — команда не распознана (возврат к ожиданию слова);
      'interrupted' — во время обработки/озвучки снова сказано «Ева»:
                      старый запрос отменён, нужно слушать новую команду.
    """
    _log('[jarvis] активация, слушаю команду...')
    emit_state('listening')

    # Каждое действие бэкенды могут дублировать — выполняем только первым
    _action_done.clear()

    # Короткий звуковой сигнал вместо голосового «Слушаю» (в hotkey-режиме
    # не нужен — там пользователь сам решил активировать клавишей; в
    # диалоговом режиме тоже — это уже продолжение разговора)
    if service.activation_mode != 'hotkey' and not dialogue:
        try:
            _play_attention_sound()
        except Exception:
            pass

    # Очищаем очередь от «хвоста» (после активации он нам не нужен —
    # команду записываем заново), но НЕ трогаем кольцевой буфер
    # audio_tail: если пользователь начал фразу в том же вдохе,
    # что и «Ева», её начало останется в буфере.
    _drain_audio_queue()

    command_text = ''
    for attempt in range(RECORD_RETRIES + 1):
        try:
            # Хвост берём только на первой попытке: при переспросе он
            # может содержать эхо нашей же озвучки «Не расслышал…».
            wav_path = record_command(
                initial_frames=list(audio_tail) if attempt == 0 else None)

            emit_state('thinking')
            command_text = transcribe(wav_path, get_whisper_model())
        except Exception as e:
            _log(f'[jarvis] ошибка записи/распознавания: {e}')
            command_text = ''

        command_text = strip_wake_word(command_text)
        _log(f'[jarvis] распознано: {command_text!r}')

        if command_text:
            break

        # В диалоговом режиме не переспрашиваем: триггером мог стать
        # фоновый шум, и голосовое «Не расслышал» — это ответ на шум.
        if dialogue:
            break

        # Не расслышали — переспрашиваем (кроме последней попытки)
        if attempt < RECORD_RETRIES:
            _log('[jarvis] не расслышал, переспрашиваю...')
            emit_state('listening')
            try:
                speak('Не расслышал. Повторите, пожалуйста.')
            except Exception:
                pass
            _drain_audio_queue()

    if not command_text:
        emit_state('idle')
        return 'empty'

    # Расширение показывает услышанную фразу на «острове» («Вы сказали: …»)
    emit_heard(command_text)

    # «Посмотреть на экран» — отдельный поток (скриншот + vision-описание):
    # это медленно (5-30 с), поэтому ДО роутера, чтобы фраза «что на экране»
    # не улетела в быстрые шаблоны и в LLM.
    if is_vision_request(command_text):
        return run_vision_flow()

    # Быстрый роутер: однозначные системные команды («открой браузер»,
    # «громкость на 50», «покажи фото кота») выполняются НАПРЯМУЮ, без
    # обращения к LLM — счёт идёт на десятки миллисекунд, а не секунды.
    fast_answer = find_fast_route(command_text)
    if fast_answer is not None:
        _log(f'[jarvis] ответ (быстрый роутер): {fast_answer!r}')
        emit_response(fast_answer)
        emit_state('speaking')
        try:
            speak(fast_answer)
        except Exception as e:
            _log(f'[jarvis] ошибка озвучки: {e}')
        emit_state('idle')
        return 'done'

    # Быстрый ответ на простые фразы («который час», «привет») — без LLM
    shortcut = find_shortcut(command_text)
    if shortcut is not None:
        _log(f'[jarvis] ответ (шорткат): {shortcut!r}')
        emit_response(shortcut)
        emit_state('speaking')
        try:
            speak(shortcut)
        except Exception as e:
            _log(f'[jarvis] ошибка озвучки: {e}')
        emit_state('idle')
        return 'done'

    # Сложная задача (написать код/сайт/текст) — сразу подтверждаем
    # голосом, чтобы пауза на размышление модели не выглядела зависанием.
    if is_complex_request(command_text):
        announce_thinking()

    # --- запрос к LLM, прерываемый словом «Ева» (не в hotkey-режиме) ---
    monitor = None
    if service.activation_mode != 'hotkey':
        monitor = InterruptMonitor(vosk_model)
        monitor.start()
    done = threading.Event()
    box = {}
    # На простые вопросы озвучиваем не больше двух коротких предложений —
    # остальное модель болтает впустую. Сложные задачи слушаем целиком.
    brief_max_sentences = 2 if not is_complex_request(command_text) else None

    def _ask():
        try:
            answer, box['streamed'] = ask_llm_streaming(
                command_text, chat_messages, holder=box,
                max_sentences=brief_max_sentences)
            box['answer'] = answer
        except Exception as e:
            _log(f'[jarvis] ошибка LLM: {e}')
            box['answer'] = None
            box['streamed'] = False
        finally:
            done.set()

    threading.Thread(target=_ask, daemon=True).start()
    deadline = time.monotonic() + MAX_LLM_WAIT_SECONDS
    while (not service.stop_event.is_set()
           and not interrupt_event.is_set()
           and not done.is_set()
           # как только речь уже пошла — дедлайн не рубит хвост ответа
           and (time.monotonic() < deadline or box.get('streamed'))):
        done.wait(0.2)
    if monitor is not None:
        monitor.stop()

    if interrupt_event.is_set():
        interrupt_event.clear()
        emit_state('idle')
        return 'interrupted'

    answer = box.get('answer')
    if answer == _TOOL_FALLBACK:
        # Модель ушла в вызов инструментов — стрим-путь не дружит с
        # tools: сбрасываем неозвученные предложения и идём обычным путём.
        _clear_voice_queue()
        answer = ask_llm(command_text, chat_messages)
        box['streamed'] = False

    if not answer:
        if not done.is_set():
            _log(f'[jarvis] LLM не ответил за {MAX_LLM_WAIT_SECONDS} с — отменяю запрос')
            return 'empty'
        answer = 'Извините, не удалось получить ответ от модели.'

    # Приводим ответ к виду живой речи: без маркдауна, дублей и лишних
    # предложений на простых вопросах (стрим уже урезал озвучку — здесь
    # то же самое для текста в меню расширения).
    answer = _humanize(answer, is_complex_request(command_text))
    _log(f'[jarvis] ответ: {answer!r}')
    emit_response(answer)

    # Обрезаем историю, чтобы не раздувать контекст (system-сообщение
    # всегда сохраняем первым).
    max_messages = 1 + HISTORY_LIMIT * 4  # с запасом на tool-сообщения
    if len(chat_messages) > max_messages:
        chat_messages[:] = [chat_messages[0]] + chat_messages[-(max_messages - 1):]

    # --- озвучка, прерываемая словом «Ева» (не в hotkey-режиме) ---
    # Если ответ уже был озвучен по предложениям во время генерации
    # (стрим-путь) — повторно не говорим.
    if not box.get('streamed'):
        monitor = None
        if service.activation_mode != 'hotkey':
            monitor = InterruptMonitor(vosk_model)
            monitor.start()
        emit_state('speaking')
        try:
            speak(answer)
        except Exception as e:
            _log(f'[jarvis] ошибка озвучки: {e}')
        if monitor is not None:
            monitor.stop()

        if interrupt_event.is_set():
            interrupt_event.clear()
            emit_state('idle')
            return 'interrupted'

    emit_state('idle')
    return 'done'


def _wake_listen(service, recognizer):
    """Ждёт слово-активатор (или ручную активацию). Возвращает True, если
    пора слушать команду."""
    while not service.stop_event.is_set():
        _unload_whisper_if_idle()
        _maybe_rewarm_ollama()
        if service.paused:
            # Пока на паузе — просто сбрасываем буфер, чтобы не копился
            try:
                audio_queue.get(timeout=0.5)
            except queue.Empty:
                pass
            if service.manual_activation_event.is_set():
                service.manual_activation_event.clear()
            continue

        if service.manual_activation_event.is_set():
            service.manual_activation_event.clear()
            return True

        if service.activation_mode == 'hotkey':
            # Режим «только по горячей клавише»: голос не слушаем, ждём
            # только ручную активацию (клавиша/меню). Очередь стравливаем,
            # чтобы не копилась.
            if service.manual_activation_event.wait(0.5):
                service.manual_activation_event.clear()
                return True
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            continue

        try:
            chunk = audio_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if recognizer.AcceptWaveform(chunk):
            result = json.loads(recognizer.Result())
            text = result.get('text', '')
        else:
            partial = json.loads(recognizer.PartialResult())
            text = partial.get('partial', '')

        if WAKE_DEBUG and text:
            _log(f'[wake] слышу: {text!r}')

        if not contains_wake_word(text):
            continue

        # Слово услышано — очищаем распознаватель и переходим к записи команды
        recognizer.Reset()
        return True

    return False


def _dialogue_listen(service, recognizer):
    """Режим непрерывного диалога (как у Алисы): после ответа слушаем
    следующую реплику БЕЗ слова-активатора.

    Возвращает True, если реплика услышана (пора обрабатывать команду);
    False — если диалог пора заканчивать: наступила тишина
    (DIALOGUE_TIMEOUT_SECONDS), нажата пауза, сказано одно «Ева» либо
    остановлен сервис.
    """
    # 1) Пауза-защита от эха: после озвучки собственный голос Евы ещё
    #    несколько мгновений идёт с колонок в микрофон. Ждём, пока либо
    #    очередь кончится (тишина наступила раньше), либо пройдёт
    #    DIALOGUE_ECHO_GUARD_SECONDS. Начало фразы пользователя, сказанной
    #    в этот момент, не теряется — его сохраняет кольцевой буфер
    #    audio_tail, и record_command подхватит его как initial_frames.
    guard_end = time.monotonic() + DIALOGUE_ECHO_GUARD_SECONDS
    while not service.stop_event.is_set() and time.monotonic() < guard_end:
        try:
            audio_queue.get(timeout=0.1)
        except queue.Empty:
            break

    recognizer.Reset()
    deadline = time.monotonic() + DIALOGUE_TIMEOUT_SECONDS
    block_seconds = BLOCK_SIZE / SAMPLE_RATE
    min_speech_blocks = max(1, round(DIALOGUE_MIN_SPEECH_SECONDS / block_seconds))
    speech_blocks = 0
    emit_state('dialog')
    while not service.stop_event.is_set():
        if service.paused:
            emit_state('idle')
            return False
        if service.manual_activation_event.is_set():
            service.manual_activation_event.clear()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            emit_state('idle')
            return False
        try:
            chunk = audio_queue.get(timeout=min(0.3, remaining))
        except queue.Empty:
            continue
        # Репликой считается только «живая» речь: непрерывный не-тихий звук
        # длительностью DIALOGUE_MIN_SPEECH_SECONDS + распознанный текст.
        # Щелчки, кашель, фоновый телевизор команду не запускают (тихие
        # блоки сбрасывают счётчик).
        if is_silence(rms(chunk)):
            speech_blocks = 0
        else:
            speech_blocks += 1
        if recognizer.AcceptWaveform(chunk):
            text = json.loads(recognizer.Result()).get('text', '')
            if text and speech_blocks >= min_speech_blocks:
                # Сказано только слово-активатор («Ева» и пауза) — это
                # не реплика, а сигнал «диалог окончен»: выходим тихо,
                # без переспроса.
                if contains_wake_word(text) and not strip_wake_word(text):
                    emit_state('idle')
                    return False
                return True
        else:
            text = json.loads(recognizer.PartialResult()).get('partial', '').strip()
            # Промежуточный текст в пару символов при живой речи — уже
            # голос, а не шум. Если в нём только слово-активатор —
            # ждём продолжения фразы.
            if (speech_blocks >= min_speech_blocks
                    and len(text) >= 2
                    and (strip_wake_word(text) or not contains_wake_word(text))):
                return True
    emit_state('idle')
    return False


def main():
    global _main_loop

    init_dbus()

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    _main_loop = GLib.MainLoop()
    try:
        _main_loop.run()
    except KeyboardInterrupt:
        service.stop_event.set()


if __name__ == '__main__':
    main()
