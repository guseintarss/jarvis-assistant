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
from jarvis.tools import desktop
from jarvis.tools import files
from jarvis.tools import search
from jarvis.tools import system


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