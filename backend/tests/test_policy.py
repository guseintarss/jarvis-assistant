"""Тесты policy engine: загрузка YAML, риски, подтверждения, пути.

Запуск (из каталога backend/):
    python -m unittest tests.test_policy -v
"""

import os
import tempfile
import unittest

from jarvis import policy as policy_mod


class TestPolicyLoad(unittest.TestCase):

    def test_defaults_without_file(self):
        p = policy_mod.Policy(data=None)
        self.assertEqual(p.allowed_roots, [os.path.expanduser('~')])
        # denylist обязателен
        self.assertIn(os.path.expanduser('~/.ssh'), p.denied_paths)
        self.assertIn('/etc', p.denied_paths)

    def test_load_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, 'policy.yaml')
            with open(cfg, 'w', encoding='utf-8') as f:
                f.write('''policy:
  allowed_roots: ["~/Docs"]
  denylist_paths: ["~/Docs/secret"]
  confirmation: {high: always, medium: never}
  tools:
    move_to_trash: {enabled: true, risk: high}
    screenshot: {enabled: false}
  sandbox: {enabled: true}
''')
            p = policy_mod.Policy.load(cfg)
            self.assertEqual(p.allowed_roots,
                             [os.path.join(os.path.expanduser('~'), 'Docs')])
            self.assertEqual(p.source, cfg)

    def test_load_missing_file_falls_back_to_defaults(self):
        p = policy_mod.Policy.load('/nonexistent/policy.yaml')
        self.assertEqual(p.allowed_roots, [os.path.expanduser('~')])

    def test_load_broken_yaml_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, 'bad.yaml')
            with open(cfg, 'w', encoding='utf-8') as f:
                f.write('policy: [невалидный yaml: {{{')
            p = policy_mod.Policy.load(cfg)
            # консервативный режим: рискованные инструменты отключены
            self.assertFalse(p.tool_enabled('move_to_trash'))
            self.assertFalse(p.tool_enabled('screenshot'))
            self.assertTrue(p.tool_enabled('volume_up'))


class TestPolicyTools(unittest.TestCase):

    def setUp(self):
        self.p = policy_mod.Policy(data={
            'tools': {'move_to_trash': {'enabled': True, 'risk': 'high'},
                      'notify': {'enabled': False}},
        })

    def test_tool_enabled_by_default(self):
        # нет записи в policy -> включён (есть в intents)
        self.assertTrue(self.p.tool_enabled('open_app'))

    def test_tool_disabled(self):
        self.assertFalse(self.p.tool_enabled('notify'))

    def test_tool_risk_default_from_intents(self):
        self.assertEqual(self.p.tool_risk('move_to_trash'), 'high')
        self.assertEqual(self.p.tool_risk('open_app'), 'low')
        self.assertEqual(self.p.tool_risk('open_url'), 'medium')

    def test_tool_risk_override(self):
        self.assertEqual(self.p.tool_risk('move_to_trash'), 'high')

    def test_unknown_tool_is_medium_risk(self):
        self.assertEqual(self.p.tool_risk('несуществующий'), 'medium')


class TestPolicyConfirmation(unittest.TestCase):

    def test_high_requires_confirmation_by_default(self):
        p = policy_mod.Policy(data=None)
        self.assertTrue(p.needs_confirmation('high'))

    def test_medium_and_low_do_not(self):
        p = policy_mod.Policy(data=None)
        self.assertFalse(p.needs_confirmation('medium'))
        self.assertFalse(p.needs_confirmation('low'))

    def test_confirmation_override(self):
        p = policy_mod.Policy(data={'confirmation': {'medium': 'always'}})
        self.assertTrue(p.needs_confirmation('medium'))


class TestPolicyPaths(unittest.TestCase):

    def setUp(self):
        self.p = policy_mod.Policy(data=None)

    def test_home_is_allowed(self):
        self.assertTrue(self.p.is_allowed(os.path.expanduser('~/Documents')))

    def test_ssh_is_denied(self):
        self.assertFalse(self.p.is_allowed(os.path.expanduser('~/.ssh')))
        self.assertTrue(self.p.is_denied(os.path.expanduser('~/.ssh/id_rsa')))

    def test_config_is_denied(self):
        self.assertFalse(self.p.is_allowed(os.path.expanduser('~/.config/foo')))

    def test_system_dirs_are_denied(self):
        self.assertFalse(self.p.is_allowed('/etc/passwd'))
        self.assertFalse(self.p.is_allowed('/usr/bin/python3'))
        self.assertTrue(self.p.is_denied('/etc'))

    def test_denied_is_stronger_than_allowed(self):
        # даже внутри allowed_roots denylist побеждает
        p = policy_mod.Policy(data={
            'allowed_roots': ['~'],
            'denylist_paths': ['~/Documents'],
        })
        self.assertFalse(p.is_allowed(os.path.expanduser('~/Documents/notes.txt')))

    def test_expanduser_and_abspath(self):
        self.assertEqual(self.p.is_denied('~/.ssh'), True)


if __name__ == '__main__':
    unittest.main()