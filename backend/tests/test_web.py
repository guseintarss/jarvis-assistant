"""Тесты ЧАСТИ 6: веб-панель (FastAPI + WebSocket + настройки + логи).

Используется TestClient (httpx). Ассистент заменяется заглушкой —
проверяем маршруты и безопасность, а не сам пайплайн. policy.yaml
для теста настроек — временный файл.
"""

import datetime
import json
import os
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient

from jarvis import policy as policy_mod
from jarvis.logger import JsonlLogger
from jarvis.proactive.reminders import ReminderStore
from jarvis.web.app import create_app


class FakeAssistant:
    """Заглушка ассистента для тестов панели."""

    def __init__(self):
        self.memory = None
        self.cloud = None
        self.processed = []

    def process(self, text):
        self.processed.append(text)
        return {'response': f'ответ: {text}', 'intent': 'chat',
                'confidence': 0.9, 'route': 'local', 'ok': True}


class FakeMemory:
    enabled = True

    def context(self):
        return {'turns': [('user', 'привет'), ('assistant', 'здравствуйте')],
                'facts': {'name': 'Алексей'}, 'last_actions': []}


class TestWebApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.policy_path = os.path.join(self.tmp, 'policy.yaml')
        with open(self.policy_path, 'w', encoding='utf-8') as f:
            f.write('policy:\n  tools:\n    open_app:\n      enabled: true\n'
                    '      risk: low\n')
        self.policy = policy_mod.Policy.load(self.policy_path)
        self.assistant = FakeAssistant()
        self.log = JsonlLogger(os.path.join(self.tmp, 'logs'), enabled=True)
        store = ReminderStore(os.path.join(self.tmp, 'reminders.db'))
        self.app = create_app(self.assistant, self.policy, log=self.log,
                              reminder_store=store)
        self.client = TestClient(self.app)

    def test_index_page(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Jarvis', r.text)

    def test_chat_post(self):
        r = self.client.post('/api/chat', json={'text': 'привет'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['response'], 'ответ: привет')
        self.assertEqual(self.assistant.processed, ['привет'])

    def test_chat_empty_text(self):
        r = self.client.post('/api/chat', json={'text': '   '})
        self.assertEqual(r.status_code, 200)  # пайплайн сам ответит

    def test_websocket_chat(self):
        with self.client.websocket_connect('/ws/chat') as ws:
            ws.send_text('который час')
            data = ws.receive_json()
        self.assertEqual(data['response'], 'ответ: который час')
        self.assertEqual(data['route'], 'local')

    def test_history_with_memory(self):
        self.assistant.memory = FakeMemory()
        r = self.client.get('/api/history')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['facts'], {'name': 'Алексей'})

    def test_history_without_memory(self):
        r = self.client.get('/api/history')
        self.assertEqual(r.json()['turns'], [])

    def test_reminders_crud(self):
        when = (datetime.datetime.now()
                + datetime.timedelta(hours=1)).isoformat()
        r = self.client.post('/api/reminders',
                             json={'when': when, 'text': 'полить цветы'})
        self.assertEqual(r.status_code, 200)
        rid = r.json()['id']

        r = self.client.get('/api/reminders')
        self.assertEqual(len(r.json()['reminders']), 1)

        r = self.client.delete(f'/api/reminders/{rid}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get('/api/reminders')
                         .json()['reminders'], [])

    def test_reminder_invalid_when(self):
        r = self.client.post('/api/reminders',
                             json={'when': 'не-дата', 'text': 'x'})
        self.assertEqual(r.status_code, 422)

    def test_settings_get(self):
        r = self.client.get('/api/settings')
        self.assertEqual(r.status_code, 200)
        tools = r.json()['tools']
        self.assertIn('open_app', tools)
        self.assertEqual(tools['open_app']['enabled'], True)

    def test_settings_update_tool_live_and_file(self):
        r = self.client.put('/api/settings/tool',
                            json={'name': 'open_app', 'enabled': False})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['enabled'])
        self.assertTrue(body['saved_to_file'])
        # файл реально изменился
        with open(self.policy_path, encoding='utf-8') as f:
            self.assertIn('enabled: false', f.read())
        # живая политика тоже
        self.assertFalse(self.policy.tool_enabled('open_app'))

    def test_settings_bad_risk_rejected(self):
        r = self.client.put('/api/settings/tool',
                            json={'name': 'open_app', 'risk': 'critical'})
        self.assertEqual(r.status_code, 422)

    def test_settings_unknown_tool(self):
        r = self.client.put('/api/settings/tool',
                            json={'name': 'нет_такого', 'enabled': False})
        self.assertEqual(r.status_code, 404)

    def test_logs_tail(self):
        self.log.event('test_event', field='value')
        r = self.client.get('/api/logs?lines=10')
        self.assertEqual(r.status_code, 200)
        kinds = [e['kind'] for e in r.json()['events']]
        self.assertIn('test_event', kinds)

    def test_status(self):
        r = self.client.get('/api/status')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('policy_source', body)
        self.assertIsNone(body['cloud'])
        self.assertIsNone(body['scheduler'])


class TestWebToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        policy = policy_mod.Policy(source='test')
        self.app = create_app(FakeAssistant(), policy)
        self.client = TestClient(self.app)

    def test_token_required(self):
        with unittest.mock.patch('jarvis.config.WEB_TOKEN', 'secret'):
            r = self.client.post('/api/chat', json={'text': 'x'})
            self.assertEqual(r.status_code, 401)
            r = self.client.post(
                '/api/chat', json={'text': 'x'},
                headers={'Authorization': 'Bearer secret'})
            self.assertEqual(r.status_code, 200)

    def test_ws_token_required(self):
        with unittest.mock.patch('jarvis.config.WEB_TOKEN', 'secret'):
            with self.assertRaises(Exception):  # отказ до accept
                with self.client.websocket_connect('/ws/chat'):
                    pass
            with self.client.websocket_connect('/ws/chat?token=secret') as ws:
                ws.send_text('x')
                data = ws.receive_json()
            self.assertEqual(data['response'], 'ответ: x')


if __name__ == '__main__':
    unittest.main()