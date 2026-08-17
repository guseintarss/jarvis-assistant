"""Реестр инструментов: единая точка вызова для исполнителя и облака.

Каждый инструмент описывается ToolSpec:
    name        — идентификатор (совпадает с intent в intents.py);
    description — человекочитаемое описание (для облачной LLM);
    params      — параметры: {имя: тип} — подсказка для генератора планов;
    fn          — вызываемая реализация (params: dict -> (ok, message[, data])).

Registry.call() оборачивает вызов: перехватывает исключения и превращает
их в вежливый ответ, чтобы ни одно исключение не «утекло» в CLI/демон.
"""

import inspect

from jarvis import intents
from jarvis.security import check_params
from jarvis.tools import calc
from jarvis.tools import clipboard
from jarvis.tools import desktop
from jarvis.tools import files
from jarvis.tools import search
from jarvis.tools import services
from jarvis.tools import system
from jarvis.tools import text
from jarvis.tools import time as time_tools
from jarvis.tools import windows


class ToolSpec:
    def __init__(self, name, description, params, fn):
        self.name = name
        self.description = description
        self.params = params          # {имя: тип} для промпта облачной LLM
        self.fn = fn

    def describe(self):
        """Однострочное описание для промпта облака."""
        params = ', '.join(f'{k}: {v}' for k, v in self.params.items())
        return f'{self.name}({params}) — {self.description}'

    def call(self, params=None, policy=None):
        """Вызывает реализацию. Инструменты поиска требуют policy."""
        kwargs = dict(params or {})
        if policy is not None and 'policy' in inspect.signature(self.fn).parameters:
            kwargs['policy'] = policy
        try:
            result = self.fn(**kwargs)
        except TypeError as exc:
            return False, f'Неверные параметры для {self.name}: {exc}', None
        except Exception as exc:  # noqa: BLE001 — инструмент не должен ронять систему
            return False, f'Ошибка в инструменте {self.name}: {exc}', None
        if not isinstance(result, tuple) or len(result) not in (2, 3):
            return bool(result), str(result), None
        if len(result) == 2:
            ok, message = result
            return ok, message, None
        ok, message, data = result
        return ok, message, data


class Registry:
    """Реестр всех инструментов ассистента."""

    def __init__(self, policy):
        self.policy = policy
        self._tools = {}
        self._register()

    def _register(self):
        add = self._tools.__setitem__

        add('open_app', ToolSpec(
            'open_app', 'открыть установленное приложение по имени',
            {'app': 'str'}, lambda app: desktop.launch_app(app)))

        add('open_file', ToolSpec(
            'open_file', 'открыть файл в системе (xdg-open)',
            {'path': 'str'}, lambda path: files.open_file(path)))

        add('open_url', ToolSpec(
            'open_url', 'открыть URL http/https в браузере',
            {'url': 'str'}, lambda url: files.open_url(url)))

        add('search_files', ToolSpec(
            'search_files', 'поиск файлов по имени (fd)',
            {'query': 'str', 'root': 'str(optional)'}, search.search_files))

        add('search_text', ToolSpec(
            'search_text', 'поиск текста в файлах (ripgrep)',
            {'query': 'str', 'root': 'str(optional)'}, search.search_text))

        add('volume_up', ToolSpec(
            'volume_up', 'увеличить громкость на N% (по умолчанию 5)',
            {'step': 'int(optional)'}, system.volume_up))

        add('volume_down', ToolSpec(
            'volume_down', 'уменьшить громкость на N% (по умолчанию 5)',
            {'step': 'int(optional)'}, system.volume_down))

        add('brightness_up', ToolSpec(
            'brightness_up', 'увеличить яркость экрана на N% (по умолчанию 5)',
            {'step': 'int(optional)'}, system.brightness_up))

        add('brightness_down', ToolSpec(
            'brightness_down', 'уменьшить яркость экрана на N% (по умолчанию 5)',
            {'step': 'int(optional)'}, system.brightness_down))

        add('notify', ToolSpec(
            'notify', 'показать уведомление на рабочем столе',
            {'message': 'str'}, lambda message, title='Jarvis':
            system.notify(message, title)))

        add('media_play_pause', ToolSpec(
            'media_play_pause', 'управление медиаплеером: play-pause/next/'
                                'previous/stop/play/pause',
            {'action': 'str(optional)'}, system.media_play_pause))

        add('lock_screen', ToolSpec(
            'lock_screen', 'заблокировать экран', {}, system.lock_screen))

        add('screenshot', ToolSpec(
            'screenshot', 'сделать скриншот экрана', {}, system.screenshot))

        add('move_to_trash', ToolSpec(
            'move_to_trash', 'переместить файл(ы) в корзину (восстановимо)',
            {'paths': 'list[str]'}, files.move_to_trash))

        # ------------------------ время (ЧАСТЬ 2) ---------------------------
        add('set_timer', ToolSpec(
            'set_timer', 'установить таймер на N (systemd-run + уведомление)',
            {'duration': 'str'}, time_tools.set_timer))
        add('set_alarm', ToolSpec(
            'set_alarm', 'установить будильник на HH:MM (systemd-run)',
            {'time': 'str(optional)', 'hour': 'str(optional)'},
            time_tools.set_alarm))
        add('set_reminder', ToolSpec(
            'set_reminder', 'напоминание на дату/время (SQLite)',
            {'time': 'str(optional)', 'day': 'str(optional)', 'text': 'str'},
            time_tools.set_reminder))
        add('check_time', ToolSpec(
            'check_time', 'текущее локальное время', {},
            time_tools.check_time))
        add('check_date', ToolSpec(
            'check_date', 'сегодняшняя дата и день недели', {},
            time_tools.check_date))
        add('list_reminders', ToolSpec(
            'list_reminders', 'список активных напоминаний', {},
            time_tools.list_reminders))
        add('cancel_reminder', ToolSpec(
            'cancel_reminder', 'отменить напоминание по id/«последнее»/«все»',
            {'target': 'str(optional)'}, time_tools.cancel_reminder))

        # ------------------------ окна (ЧАСТЬ 2) ----------------------------
        add('minimize_window', ToolSpec(
            'minimize_window', 'свернуть окно приложения (wmctrl)',
            {'window': 'str'}, windows.minimize_window))
        add('maximize_window', ToolSpec(
            'maximize_window', 'развернуть окно приложения (wmctrl)',
            {'window': 'str'}, windows.maximize_window))
        add('close_window', ToolSpec(
            'close_window', 'закрыть окно приложения (wmctrl)',
            {'window': 'str'}, windows.close_window))
        add('list_windows', ToolSpec(
            'list_windows', 'список открытых окон (wmctrl -l)', {},
            windows.list_windows))
        add('switch_window', ToolSpec(
            'switch_window', 'переключиться на окно приложения (wmctrl)',
            {'window': 'str'}, windows.switch_window))
        add('switch_workspace', ToolSpec(
            'switch_workspace', 'переключить рабочий стол (wmctrl -s)',
            {'number': 'str(optional)'}, windows.switch_workspace))

        # ------------------------ текст (ЧАСТЬ 2) ---------------------------
        add('count_words', ToolSpec(
            'count_words', 'посчитать слова в тексте',
            {'text': 'str'}, text.count_words))
        add('change_case', ToolSpec(
            'change_case', 'сменить регистр текста (верхний/нижний)',
            {'case': 'str(optional)', 'text': 'str'}, text.change_case))
        add('translate_text', ToolSpec(
            'translate_text', 'перевести текст на язык (бесплатный API)',
            {'lang': 'str(optional)', 'text': 'str'}, text.translate_text))

        # ------------------------ система (ЧАСТЬ 2) -------------------------
        add('system_info', ToolSpec(
            'system_info', 'информация о системе (OS, CPU, RAM, аптайм)',
            {}, system.system_info))
        add('check_disk', ToolSpec(
            'check_disk', 'свободное место на диске', {}, system.check_disk))
        add('check_battery', ToolSpec(
            'check_battery', 'заряд батареи ноутбука', {},
            system.check_battery))
        add('check_network', ToolSpec(
            'check_network', 'проверка сети и интернета', {},
            system.check_network))
        add('list_processes', ToolSpec(
            'list_processes', 'топ процессов по памяти',
            {'n': 'str(optional)'}, system.list_processes))
        add('kill_process', ToolSpec(
            'kill_process', 'завершить процесс по PID (SIGTERM)',
            {'pid': 'str'}, system.kill_process))

        # ------------------------ калькулятор (ЧАСТЬ 2) ---------------------
        add('calculate', ToolSpec(
            'calculate', 'математические вычисления (безопасный парсер)',
            {'expression': 'str'}, calc.calculate))
        add('convert_currency', ToolSpec(
            'convert_currency', 'конвертация валют (кэш + API)',
            {'amount': 'str', 'from': 'str', 'to': 'str'},
            lambda **kw: calc.convert_currency(
                amount=kw.get('amount'), from_=kw.get('from'),
                to_=kw.get('to'))))
        add('convert_units', ToolSpec(
            'convert_units', 'конвертация единиц измерения',
            {'amount': 'str', 'from': 'str', 'to': 'str'},
            lambda **kw: calc.convert_units(
                amount=kw.get('amount'), from_=kw.get('from'),
                to_=kw.get('to'))))

        # ------------------------ буфер обмена (ЧАСТЬ 2) --------------------
        add('clipboard_copy', ToolSpec(
            'clipboard_copy', 'скопировать текст в буфер обмена',
            {'text': 'str(optional)'}, clipboard.clipboard_copy))
        add('clipboard_paste', ToolSpec(
            'clipboard_paste', 'показать содержимое буфера обмена', {},
            clipboard.clipboard_paste))
        add('clipboard_history', ToolSpec(
            'clipboard_history', 'история копирований в буфер обмена', {},
            clipboard.clipboard_history))

        # ------------------------ сервисы (ЧАСТЬ 2) -------------------------
        add('check_weather', ToolSpec(
            'check_weather', 'погода в городе (wttr.in)',
            {'city': 'str(optional)'}, services.check_weather))
        add('check_news', ToolSpec(
            'check_news', 'свежие новости из RSS', {}, services.check_news))
        add('send_email', ToolSpec(
            'send_email', 'отправить письмо по SMTP (подтверждение)',
            {'to': 'str(optional)', 'text': 'str'}, services.send_email))
        add('check_calendar', ToolSpec(
            'check_calendar', 'ближайшие события из локального .ics', {},
            services.check_calendar))

    # ------------------------------ API -----------------------------------

    def names(self):
        return sorted(self._tools)

    def get(self, name):
        return self._tools.get(name)

    def enabled_names(self):
        """Имена инструментов, разрешённых политикой (для облака и executor)."""
        return sorted(n for n in self._tools if self.policy.tool_enabled(n))

    def describe_all(self):
        """Описания для системного промпта облачной LLM."""
        return [self._tools[n].describe() for n in self.enabled_names()]

    def call(self, name, params=None, guard=None):
        """Вызов инструмента через политику и (при наличии) PathGuard.

        Возвращает (ok, message, data). Запрещённые/неизвестные инструменты
        отклоняются до вызова реализации.
        """
        tool = self._tools.get(name)
        if tool is None:
            return False, f'Инструмент «{name}» не существует.', None
        if not self.policy.tool_enabled(name):
            return False, f'Инструмент «{name}» отключён политикой.', None

        params = dict(params or {})
        # Проверка путей/URL политикой перед выполнением (доп. страховка:
        # сам инструмент guard не видит, поэтому проверяем здесь и в executor)
        if guard is not None:
            ok, reason, params = check_params(params, guard)
            if not ok:
                return False, f'Отказано: {reason}', None

        return tool.call(params, policy=self.policy)