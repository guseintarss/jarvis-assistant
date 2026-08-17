"""Тесты executor: политика, подтверждения, пути, планы облака.

Реализации инструментов подменяются моками — реальная громкость,
файлы и приложения не трогаются. Проверяем САМО исполнение решений:
что отклоняется, что разрешается, как исполняются облачные планы.

Запуск (из каталога backend/):
    python -m unittest tests.test_executor -v
"""

import os
import tempfile
import unittest
from unittest import mock

from jarvis import policy as policy_mod
from jarvis.executor import Executor
from jarvis.security import Confirmator, PathGuard
from jarvis.tools.registry import Registry, ToolSpec


def _make_env(tmp_root, confirmation=None, tools_override=None):
    data = {
        'allowed_roots': [tmp_root],
        'denylist_paths': [os.path.join(tmp_root, 'secret'),
                           os.path.expanduser('~/.ssh')],
        'tools': tools_override or {},
        'sandbox': {'enabled': True, 'code_max_chars': 5000,
                    'time_limit_sec': 10, 'memory_limit_mb': 256},
    }
    if confirmation:
        data['confirmation'] = confirmation
    policy = policy_mod.Policy(data=data)
    registry = Registry(policy)
    guard = PathGuard(policy)
    confirmator = Confirmator(policy, prompt_fn=lambda q: 'y')
    executor = Executor(policy, registry, guard, confirmator)
    return policy, registry, guard, confirmator, executor


class TestExecutorLocal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_intent(self):
        _, _, _, _, ex = _make_env(self.root)
        ok, message, _ = ex.run_intent('несуществующий')
        self.assertFalse(ok)

    def test_disabled_tool_blocked(self):
        _, _, _, _, ex = _make_env(
            self.root, tools_override={'notify': {'enabled': False}})
        ok, message, _ = ex.run_intent('notify')
        self.assertFalse(ok)
        self.assertIn('отключён', message)

    def test_low_risk_tool_executes_without_confirmation(self):
        _, registry, _, _, ex = _make_env(self.root)
        with mock.patch.object(registry, 'call',
                               return_value=(True, 'Готово.', None)) as m:
            ok, message, _ = ex.run_intent('lock_screen')
            m.assert_called_once()
        self.assertTrue(ok)

    def test_high_risk_confirmed_executes(self):
        _, registry, _, _, ex = _make_env(self.root)
        with mock.patch.object(registry, 'call',
                               return_value=(True, 'В корзине.', None)) as m:
            ok, message, _ = ex.run_intent('move_to_trash',
                                           {'path': os.path.join(self.root, 'a.txt')})
            m.assert_called_once()
        self.assertTrue(ok)

    def test_high_risk_denied_blocks(self):
        _, registry, _, _, ex = _make_env(self.root)
        ex.confirmator = Confirmator(ex.policy, prompt_fn=lambda q: 'n')
        with mock.patch.object(registry, 'call') as m:
            ok, message, _ = ex.run_intent('move_to_trash', {'path': 'x'})
            m.assert_not_called()
        self.assertFalse(ok)
        self.assertIn('отклонено', message)

    def test_path_in_denylist_blocked(self):
        _, registry, _, _, ex = _make_env(self.root)
        with mock.patch.object(registry, 'call') as m:
            ok, message, _ = ex.run_intent(
                'move_to_trash', {'path': os.path.join(self.root, 'secret', 'f')})
            m.assert_not_called()
        self.assertFalse(ok)
        self.assertIn('Отказано', message)


class TestPlanValidation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self):
        return _make_env(self.root)

    def test_valid_plan_executes(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'explanation': 'проверка', 'steps': [
            {'tool': 'notify', 'params': {'message': 'тест'}},
        ]}
        ok, err, clean = validate_plan(plan, ex.policy, registry, guard)
        self.assertTrue(ok, err)
        with mock.patch.object(registry, 'call',
                               return_value=(True, 'ok', None)) as m:
            ok, message, results = ex.run_plan(clean)
        self.assertTrue(ok)
        m.assert_called_once_with('notify', {'message': 'тест'}, guard=guard)

    def test_plan_with_unknown_tool_rejected(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'steps': [{'tool': 'rm_rf', 'params': {}}]}
        ok, err, _ = validate_plan(plan, ex.policy, registry, guard)
        self.assertFalse(ok)
        self.assertIn('неизвестный инструмент', err)

    def test_plan_with_raw_command_rejected(self):
        """Модель попыталась выполнить «сырую команду» — валидатор обязан
        отклонить план целиком."""
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'steps': [
            {'tool': 'notify', 'params': {'message': 'x'},
             'command': 'rm -rf ~/Documents'},
        ]}
        ok, err, _ = validate_plan(plan, ex.policy, registry, guard)
        self.assertFalse(ok)
        self.assertIn('запрещённое поле', err)

    def test_plan_with_shell_key_rejected(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'steps': [
            {'tool': 'open_app', 'params': {'app': 'x',
                                            'shell': 'echo hacked'}},
        ]}
        ok, err, _ = validate_plan(plan, ex.policy, registry, guard)
        self.assertFalse(ok)

    def test_plan_path_outside_allowed_rejected(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'steps': [
            {'tool': 'move_to_trash',
             'params': {'paths': ['/etc/hosts']}},
        ]}
        ok, err, _ = validate_plan(plan, ex.policy, registry, guard)
        self.assertFalse(ok)

    def test_plan_too_many_steps_rejected(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        steps = [{'tool': 'notify', 'params': {'message': 'x'}}] * 99
        ok, err, _ = validate_plan({'steps': steps}, ex.policy, registry, guard)
        self.assertFalse(ok)

    def test_run_code_step_only_with_content(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        # без поля content — отказ
        ok, err, _ = validate_plan(
            {'steps': [{'tool': 'run_code', 'params': {}}]},
            ex.policy, registry, guard)
        self.assertFalse(ok)
        # с content — валиден (выполнится в песочнице)
        ok, err, clean = validate_plan(
            {'steps': [{'tool': 'run_code',
                        'params': {'content': 'print(1)'}}]},
            ex.policy, registry, guard)
        self.assertTrue(ok, err)

    def test_fail_fast_stops_plan(self):
        _, registry, guard, _, ex = _make_env(self.root)
        from jarvis.plan import validate_plan
        plan = {'steps': [
            {'tool': 'notify', 'params': {'message': 'first'}},
            {'tool': 'notify', 'params': {'message': 'second'}},
        ]}
        ok, _, clean = validate_plan(plan, ex.policy, registry, guard)
        self.assertTrue(ok)
        calls = []
        def fake_call(name, params=None, guard=None):
            calls.append(name)
            if name == 'notify' and params.get('message') == 'first':
                return False, 'упало', None
            return True, 'ok', None
        registry.call = fake_call
        ok, message, results = ex.run_plan(clean)
        self.assertFalse(ok)
        self.assertEqual(calls, ['notify'])  # второй шаг не выполнен


class TestExecutorPlanRisk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_high_risk_plan_step_asks_confirmation(self):
        """План с move_to_trash требует подтверждения даже если в плане
        confirm не выставлен."""
        _, registry, guard, _, ex = _make_env(self.root)
        ex.confirmator = Confirmator(ex.policy, prompt_fn=lambda q: 'n')
        from jarvis.plan import validate_plan
        plan = {'steps': [
            {'tool': 'move_to_trash', 'params': {'paths': [self.root + '/a.txt']}},
        ]}
        ok, _, clean = validate_plan(plan, ex.policy, registry, guard)
        self.assertTrue(ok)
        with mock.patch.object(registry, 'call') as m:
            ok, message, _ = ex.run_plan(clean)
        self.assertFalse(ok)
        m.assert_not_called()


if __name__ == '__main__':
    unittest.main()