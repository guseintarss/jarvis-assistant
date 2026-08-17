"""Тесты security: PathGuard (allow/deny, симлинки) и Confirmator.

Запуск (из каталога backend/):
    python -m unittest tests.test_security -v
"""

import os
import tempfile
import unittest

from jarvis import policy as policy_mod
from jarvis.security import Confirmator, PathGuard


class TestPathGuard(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.policy = policy_mod.Policy(data={
            'allowed_roots': [self.root],
            'denylist_paths': [os.path.join(self.root, 'secret'),
                               os.path.expanduser('~/.ssh')],
        })
        self.guard = PathGuard(self.policy)

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowed_path(self):
        ok, reason, resolved = self.guard.guard(os.path.join(self.root, 'a.txt'))
        self.assertTrue(ok)
        self.assertEqual(resolved, os.path.join(self.root, 'a.txt'))

    def test_denied_path(self):
        ok, reason, _ = self.guard.guard(os.path.join(self.root, 'secret', 'k'))
        self.assertFalse(ok)
        self.assertIn('запрещённых', reason)

    def test_outside_allowed_roots(self):
        ok, reason, _ = self.guard.guard('/etc/passwd')
        self.assertFalse(ok)
        self.assertIn('вне разрешённых', reason)

    def test_empty_path(self):
        ok, reason, _ = self.guard.guard('')
        self.assertFalse(ok)

    def test_symlink_to_denied_is_blocked(self):
        # симлинк на ~/.ssh не должен обходить guard (realpath)
        link = os.path.join(self.root, 'link')
        os.symlink(os.path.expanduser('~/.ssh'), link)
        ok, reason, _ = self.guard.guard(link)
        self.assertFalse(ok)

    def test_nonexistent_path_checks_parent(self):
        ok, _, _ = self.guard.guard(os.path.join(self.root, 'secret', 'new.txt'))
        self.assertFalse(ok)

    def test_guards_list(self):
        ok, _, ok_paths = self.guard.guards(
            [os.path.join(self.root, 'a'), os.path.join(self.root, 'b')])
        self.assertTrue(ok)
        self.assertEqual(len(ok_paths), 2)
        ok, reason, _ = self.guard.guards(
            [os.path.join(self.root, 'a'), os.path.join(self.root, 'secret')])
        self.assertFalse(ok)


class TestConfirmator(unittest.TestCase):

    def setUp(self):
        self.policy = policy_mod.Policy(data=None)

    def test_low_risk_no_prompt_needed(self):
        c = Confirmator(self.policy, prompt_fn=lambda q: 'n')
        self.assertFalse(c.needs('open_app'))          # low
        self.assertTrue(c.confirm('open_app', ''))     # не спрашивает

    def test_high_risk_asks_and_denies(self):
        c = Confirmator(self.policy, prompt_fn=lambda q: 'n')
        self.assertTrue(c.needs('move_to_trash'))
        self.assertFalse(c.confirm('move_to_trash', ''))

    def test_high_risk_confirmed(self):
        c = Confirmator(self.policy, prompt_fn=lambda q: 'y')
        self.assertTrue(c.confirm('move_to_trash', ''))

    def test_non_interactive_denies(self):
        # демон: prompt_fn=None — безопасный отказ
        c = Confirmator(self.policy, prompt_fn=None)
        self.assertFalse(c.confirm('move_to_trash', ''))

    def test_auto_yes_grants(self):
        c = Confirmator(self.policy, prompt_fn=None, auto_yes=True)
        self.assertTrue(c.confirm('move_to_trash', ''))

    def test_session_memory(self):
        # одно «да» — и в этой сессии инструмент больше не спрашивает
        answers = iter(['y', 'n'])
        c = Confirmator(self.policy, prompt_fn=lambda q: next(answers))
        self.assertTrue(c.confirm('move_to_trash', ''))
        self.assertTrue(c.confirm('move_to_trash', ''))  # из кэша сессии


class TestRedact(unittest.TestCase):

    def test_redacts_secret_values(self):
        from jarvis.logger import redact
        self.assertIn('***REDACTED***',
                      redact('password=supersecret value'))
        self.assertNotIn('supersecret', redact('password=supersecret'))

    def test_redacts_secret_keys(self):
        from jarvis.logger import redact
        out = redact({'api_key': 'abc123', 'message': 'привет'})
        self.assertEqual(out['api_key'], '***REDACTED***')
        self.assertEqual(out['message'], 'привет')


if __name__ == '__main__':
    unittest.main()