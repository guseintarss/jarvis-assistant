"""Инструменты управления окнами через wmctrl.

Внимание: wmctrl работает только на X11/XWayland. На «чистом» Wayland
(GNOME) переключение окон через wmctrl недоступно — инструмент честно
об этом сообщает (управление окнами GNOME можно добавить расширением,
см. gnome-extension/). Никаких shell-строк: только списки аргументов.
"""

import shutil

from jarvis.tools.system import _run

# Действия wmctrl, допустимые для команды -r <окно>
_ACTIONS = {
    'minimize': ['-b', 'add,hidden'],
    'maximize': ['-b', 'add,maximized_vert,maximized_horz'],
}


def _have_wmctrl():
    if not shutil.which('wmctrl'):
        return False, ('Утилита wmctrl не установлена. Установите её '
                       '(Arch: wmctrl) — управление окнами работает на '
                       'X11/XWayland.')
    return True, ''


def _window_change(action, window):
    """Свернуть/развернуть окно приложения."""
    ok, msg = _have_wmctrl()
    if not ok:
        return False, msg
    target = window.strip() or None
    rc, _, err = _run(['wmctrl', '-r', target or '',
                       *_ACTIONS[action]])
    if rc == 0:
        verb = 'свернул' if action == 'minimize' else 'развернул'
        what = f' окно «{target}»' if target else ''
        return True, f'{verb.capitalize()}{what}.'
    return False, f'wmctrl не сработал: {err.strip()[:200]}'


def minimize_window(window=''):
    """Свернуть окно (по имени приложения, если указано)."""
    if not window.strip():
        return False, 'Скажите, какое окно свернуть (например, «сверни окно браузера»).'
    return _window_change('minimize', window)


def maximize_window(window=''):
    """Развернуть окно на весь экран."""
    if not window.strip():
        return False, 'Скажите, какое окно развернуть.'
    return _window_change('maximize', window)


def close_window(window=''):
    """Закрыть окно приложения (wmctrl -c)."""
    ok, msg = _have_wmctrl()
    if not ok:
        return False, msg
    target = window.strip()
    if not target:
        return False, 'Скажите, какое окно закрыть (например, «закрой окно плеера»).'
    rc, _, err = _run(['wmctrl', '-c', target])
    if rc == 0:
        return True, f'Закрываю окно «{target}».'
    return False, f'wmctrl не сработал: {err.strip()[:200]}'


def list_windows():
    """Список открытых окон (wmctrl -l)."""
    ok, msg = _have_wmctrl()
    if not ok:
        return False, msg
    rc, out, err = _run(['wmctrl', '-l'])
    if rc != 0:
        return False, f'wmctrl не сработал: {err.strip()[:200]}'
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return True, 'Открытых окон не нашёл (или Wayland не отдаёт их wmctrl).'
    return True, 'Открытые окна:\n' + '\n'.join(
        f'{i + 1}) {line.split(None, 3)[-1] if len(line.split()) > 3 else line}'
        for i, line in enumerate(lines))


def switch_window(window=''):
    """Переключиться на окно приложения (wmctrl -a)."""
    ok, msg = _have_wmctrl()
    if not ok:
        return False, msg
    target = window.strip()
    if not target:
        return False, 'Скажите, на какое окно переключиться.'
    rc, _, err = _run(['wmctrl', '-a', target])
    if rc == 0:
        return True, f'Переключился на окно «{target}».'
    return False, (f'Не нашёл окно «{target}»: {err.strip()[:200]}')


def switch_workspace(number=None):
    """Переключить рабочий стол (wmctrl -s <N>)."""
    ok, msg = _have_wmctrl()
    if not ok:
        return False, msg
    try:
        ws = int(number or 1) - 1
        if ws < 0:
            ws = 0
    except (TypeError, ValueError):
        return False, 'Скажите номер рабочего стола числом.'
    rc, _, err = _run(['wmctrl', '-s', str(ws)])
    if rc == 0:
        return True, f'Переключился на рабочий стол {ws + 1}.'
    return False, f'wmctrl не сработал: {err.strip()[:200]}'


TOOLS = {
    'minimize_window': minimize_window,
    'maximize_window': maximize_window,
    'close_window': close_window,
    'list_windows': list_windows,
    'switch_window': switch_window,
    'switch_workspace': switch_workspace,
}