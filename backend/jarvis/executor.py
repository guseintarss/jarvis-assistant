"""Исполнитель: выполняет локальные намерения и облачные JSON-планы.

Порядок для каждого шага:
    1. инструмент существует и включён политикой (Registry.call);
    2. пути проходят PathGuard (denylist сильнее allowlist);
    3. если риск требует подтверждения — спрашиваем (Confirmator);
    4. только после этого вызывается реализация;
    5. результат логируется в JSONL.

Облачный план исполняется строго последовательно и останавливается
на первом неудачном шаге (fail-fast) — так частично выполненный план
не маскируется «успехом».
"""

from jarvis import intents
from jarvis import logger
from jarvis.sandbox import bwrap
from jarvis.security import check_params


class Executor:
    """Исполнитель намерений и планов."""

    def __init__(self, policy, registry, guard, confirmator, log=None):
        self.policy = policy
        self.registry = registry
        self.guard = guard
        self.confirmator = confirmator
        self.log = log or logger.get_logger()

    # --------------------------- намерения ---------------------------------

    def run_intent(self, intent_name, slots=None):
        """Локальное намерение -> (ok, message, data).

        Возвращает также пометку, что запрос нужно отправить в облако
        (chat) — это решает pipeline.
        """
        slots = slots or {}
        intent = intents.INTENTS.get(intent_name)
        if intent is None:
            return False, f'Неизвестное намерение: {intent_name}', None
        if intent.fallback:
            return False, '', None  # chat — маршрут в облачную LLM

        if not self.policy.tool_enabled(intent_name):
            self.log.event('blocked', intent=intent_name,
                           reason='disabled by policy')
            return False, f'Инструмент «{intent_name}» отключён политикой.', None

        risk = self.policy.tool_risk(intent_name)
        description = f'риск {intents.RISK_LABELS[risk]}'
        if not self.confirmator.confirm(intent_name, description):
            self.log.event('denied', intent=intent_name, risk=risk)
            return False, (f'Действие «{intent_name}» требует подтверждения '
                           'и было отклонено.'), None

        # пути/URL проходят guard ДО вызова инструмента
        ok, reason, clean_slots = check_params(slots, self.guard)
        if not ok:
            self.log.event('blocked', intent=intent_name, reason=reason)
            return False, f'Отказано: {reason}', None

        ok, message, data = self.registry.call(intent_name, clean_slots,
                                               guard=self.guard)
        self.log.event('executed', intent=intent_name, risk=risk,
                       ok=ok, message=message)
        return ok, message, data

    # --------------------------- планы -------------------------------------

    def run_plan(self, plan):
        """Облачный план -> (ok, message, step_results)."""
        results = []
        for step in plan['steps']:
            tool = step['tool']
            params = step['params']

            if tool == 'run_code':
                ok, message = self._run_code_step(params)
                results.append({'tool': tool, 'ok': ok, 'message': message})
                if not ok:
                    return False, f'Шаг «run_code» не выполнен: {message}', results
                continue

            risk = self.policy.tool_risk(tool)
            if step.get('confirm') or self.confirmator.needs(tool, risk):
                if not self.confirmator.confirm(
                        tool, f'риск {intents.RISK_LABELS[risk]} '
                              f'({step.get("reason") or "шаг плана"})'):
                    self.log.event('denied', intent=tool, risk=risk,
                                   source='cloud_plan')
                    return False, f'Шаг «{tool}» отклонён пользователем.', results

            ok, reason, clean_params = check_params(params, self.guard)
            if not ok:
                self.log.event('blocked', intent=tool, source='cloud_plan',
                               reason=reason)
                return False, f'Шаг «{tool}»: {reason}', results

            ok, message, data = self.registry.call(tool, clean_params,
                                                   guard=self.guard)
            self.log.event('executed', intent=tool, risk=risk,
                           source='cloud_plan', ok=ok, message=message,
                           params=params)
            results.append({'tool': tool, 'ok': ok, 'message': message})
            if not ok:
                return False, f'Шаг «{tool}» не выполнен: {message}', results

        return True, 'План выполнен.', results

    def _run_code_step(self, params):
        """Запуск сгенерированного кода ТОЛЬКО в sandbox."""
        if not self.policy.sandbox.get('enabled', True):
            return False, 'Выполнение кода отключено политикой (sandbox.enabled=false).'
        if not self.confirmator.confirm(
                'run_code',
                'запуск сгенерированного кода в песочнице (bubblewrap)'):
            return False, 'Запуск кода отклонён: нет подтверждения.'
        self.log.event('sandbox_start', code_chars=len(params.get('content', '')))
        ok, message = bwrap.run_code(params.get('content', ''), self.policy)
        self.log.event('sandbox_done', ok=ok, message=message)
        return ok, message