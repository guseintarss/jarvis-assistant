"""Модуль безопасности: allow/deny путей, подтверждения, редэкция.

Здесь собраны все «стражи» между пользовательским запросом и системой:

    PathGuard     — проверка путей против policy (denylist сильнее allowlist);
    Confirmator   — подтверждение опасных действий перед исполнением;
    redact        — редэкция секретов в логах (см. logger).

Важно: проверяется РЕАЛЬНЫЙ путь (после раскрытия симлинков). Симлинк
~/.ssh на ~/Documents/* не обманет страж: realpath возвращает настоящее
расположение, и оно попадает в denylist.
"""

import os

from jarvis import logger


def _resolve(path):
    """Раскрывает ~, делает абсолютным и резолвит симлинки."""
    expanded = os.path.abspath(os.path.expanduser(path))
    try:
        return os.path.realpath(expanded)
    except OSError:
        return expanded


class PathGuard:
    """Проверяет пути на соответствие политике allow/deny."""

    def __init__(self, policy):
        self.policy = policy

    def guard(self, path):
        """Возвращает (ok, reason, resolved_path).

        ok=False, если путь вне allowed_roots или внутри denylist.
        Для несуществующих путей проверяем родительский каталог —
        так «удалить ещё не созданный файл» тоже блокируется корректно.
        """
        if not path or not str(path).strip():
            return False, 'пустой путь', ''
        target = _resolve(str(path))
        for check in (target, os.path.dirname(target) or target):
            if not self.policy.is_allowed(check):
                denied = self.policy.is_denied(check)
                reason = ('путь в списке запрещённых: %s' % check) if denied \
                    else ('путь вне разрешённых корней: %s' % check)
                return False, reason, target
            break
        return True, '', target

    def guards(self, paths):
        """Проверяет список путей; возвращает (ok, reason, ok_paths)."""
        ok_paths, bad = [], []
        for p in paths or []:
            ok, reason, resolved = self.guard(p)
            if ok:
                ok_paths.append(resolved)
            else:
                bad.append((p, reason))
        if bad:
            return False, '; '.join(f'{p}: {r}' for p, r in bad[:5]), ok_paths
        return True, '', ok_paths


# Имена параметров инструментов, которые содержат пути / URL
PATH_KEYS = ('path', 'paths', 'file', 'files', 'root', 'dir', 'target')
URL_KEYS = ('url', 'link')


def check_params(params, guard):
    """Проверяет пути и URL в параметрах инструмента (доп. страховка
    перед вызовом реализации). Возвращает (ok, reason, cleaned_params)."""
    params = dict(params or {})
    for key in PATH_KEYS:
        if key in params:
            values = params[key] if isinstance(params[key], list) \
                else [params[key]]
            ok, reason, ok_values = guard.guards([str(v) for v in values])
            if not ok:
                return False, reason, None
            params[key] = ok_values if isinstance(params[key], list) \
                else ok_values[0]
    for key in URL_KEYS:
        if key in params:
            from jarvis.tools.files import validate_url
            ok, reason, cleaned = validate_url(str(params[key]))
            if not ok:
                return False, reason, None
            params[key] = cleaned
    return True, '', params


class Confirmator:
    """Подтверждение опасных действий.

    prompt_fn(text) -> bool — функция вопроса у пользователя. В CLI это
    input() с y/n; в D-Bus демоне — автозапрет (неинтерактивный режим),
    потому что демон не может ждать ответа посреди сессии.
    """

    def __init__(self, policy, prompt_fn=None, auto_yes=False):
        self.policy = policy
        self._prompt_fn = prompt_fn
        self.auto_yes = auto_yes          # JARVIS_ASSUME_YES=1 (для тестов/автоматизации)
        self._session_yes = set()         # инструменты, подтверждённые на сессию

    def needs(self, tool_name, risk=None):
        """Нужно ли спрашивать перед вызовом инструмента."""
        if risk is None:
            risk = self.policy.tool_risk(tool_name)
        return self.policy.needs_confirmation(risk)

    def confirm(self, tool_name, description):
        """Спрашивает пользователя. Возвращает True/False.

        Неинтерактивно (нет prompt_fn) -> False (безопасный отказ),
        событие уходит в лог как 'confirmation_denied'.
        """
        if not self.needs(tool_name):
            return True
        if self.auto_yes:
            self._session_yes.add(tool_name)
            return True
        if tool_name in self._session_yes:
            return True
        if self._prompt_fn is None:
            logger.get_logger().event(
                'confirmation_denied', tool=tool_name,
                reason='non-interactive: требуется подтверждение')
            return False
        answer = self._prompt_fn(
            f'[Jarvis] Действие «{tool_name}» — {description}. Выполнить? [y/N] ')
        granted = str(answer).strip().lower() in ('y', 'yes', 'д', 'да')
        if granted:
            self._session_yes.add(tool_name)
        logger.get_logger().event('confirmation',
                                  tool=tool_name, granted=granted)
        return granted