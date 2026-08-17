"""Тесты ЧАСТИ 5: цепочка облачных провайдеров, rate limiter, роутер.

HTTP провайдеров мокается на уровне requests — реальные сети/ключи
не используются. Валидация планов — на настоящем Policy/Registry.
"""

import json
import unittest
from unittest import mock

from jarvis import policy as policy_mod
from jarvis import plan as plan_mod
from jarvis.cloud import llm as llm_mod
from jarvis.cloud.rate_limiter import RateLimiter
from jarvis.cloud.router import CloudRouter
from jarvis.cloud.providers import base as base_mod
from jarvis.cloud.providers import gigachat as gigachat_mod
from jarvis.security import PathGuard
from jarvis.tools.registry import Registry


class FakeResponse:
    """Минимальный ответ requests.Response."""

    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise base_mod.requests.HTTPError(
                f'{self.status_code} error', response=self)

    def json(self):
        return self._data


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.limiter = RateLimiter(max_per_minute=3, cooldown_sec=60,
                                   cooldown_failures=2,
                                   now_fn=lambda: self.clock[0])

    def test_window_limit(self):
        for _ in range(3):
            self.assertTrue(self.limiter.allow('p'))
        self.assertFalse(self.limiter.allow('p'))

    def test_window_rotates(self):
        for _ in range(3):
            self.limiter.allow('p')
        self.clock[0] += 61
        self.assertTrue(self.limiter.allow('p'))

    def test_cooldown_after_failures(self):
        self.limiter.allow('p')
        self.limiter.failure('p')
        self.limiter.allow('p')
        self.limiter.failure('p')
        # cooldown активен
        self.assertFalse(self.limiter.allow('p'))
        self.clock[0] += 61
        self.assertTrue(self.limiter.allow('p'))

    def test_success_resets_failures(self):
        self.limiter.allow('p')
        self.limiter.failure('p')
        self.limiter.success('p')
        self.limiter.allow('p')  # счётчик неудач сброшен — запрос прошёл
        self.assertTrue(self.limiter.status('p')['failures'] == 0)


class StubProvider:
    """Провайдер-заглушка для тестов роутера."""

    def __init__(self, name, configured=True, ok=False, content='',
                 error='ошибка'):
        self.name = name
        self._configured = configured
        self._result = (ok, content, error)
        self.requests = 0

    def is_configured(self):
        return self._configured

    def request(self, payload):
        self.requests += 1
        return self._result


class TestCloudRouter(unittest.TestCase):
    def test_skips_unconfigured(self):
        router = CloudRouter(
            providers=[StubProvider('a', configured=False),
                       StubProvider('b', ok=True, content='x')],
            limiter=RateLimiter())
        ok, content, error, provider = router.request({})
        self.assertTrue(ok)
        self.assertEqual(provider, 'b')
        self.assertEqual(router.stats['a']['skipped'], 1)

    def test_fallback_chain(self):
        router = CloudRouter(
            providers=[StubProvider('a', ok=False, error='нет сети'),
                       StubProvider('b', ok=True, content='ok')],
            limiter=RateLimiter())
        ok, content, error, provider = router.request({})
        self.assertTrue(ok)
        self.assertEqual(provider, 'b')
        self.assertEqual(content, 'ok')
        self.assertEqual(router.stats['a']['fail'], 1)

    def test_all_fail_returns_last_error(self):
        router = CloudRouter(
            providers=[StubProvider('a', ok=False, error='e1'),
                       StubProvider('b', ok=False, error='e2')],
            limiter=RateLimiter())
        ok, content, error, provider = router.request({})
        self.assertFalse(ok)
        self.assertEqual(provider, 'b')
        self.assertIn('e2', error)

    def test_attempts_yield_each(self):
        router = CloudRouter(
            providers=[StubProvider('a', ok=False, error='x'),
                       StubProvider('b', ok=True, content='y')],
            limiter=RateLimiter())
        results = list(router.attempts({}))
        self.assertEqual([(n, ok) for n, ok, _, _ in results],
                         [('a', False), ('b', True)])

    def test_chain_names(self):
        router = CloudRouter(providers=[StubProvider('a'), StubProvider('b')],
                             limiter=RateLimiter())
        self.assertEqual(router.chain(), ['a', 'b'])


class TestOpenAICompatProvider(unittest.TestCase):
    def test_headers_and_model_injected(self):
        prov = base_mod.OpenAICompatProvider()
        prov.name = 'test'
        prov.base_url = 'https://x.test/v1'
        prov.model = 'm-1'
        prov.api_key = 'secret-key'
        resp = FakeResponse(200, {'choices': [{'message': {'content': 'привет'}}]})
        with mock.patch.object(prov._session, 'post',
                               return_value=resp) as post:
            ok, content, error = prov.request({'messages': []})
        self.assertTrue(ok)
        self.assertEqual(content, 'привет')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://x.test/v1/chat/completions')
        self.assertEqual(kwargs['json']['model'], 'm-1')
        self.assertEqual(kwargs['headers']['Authorization'],
                         'Bearer secret-key')

    def test_429_retries_then_fails(self):
        prov = base_mod.OpenAICompatProvider()
        prov.name = 'test'
        prov.base_url = 'https://x.test/v1'
        prov.model = 'm'
        resp = FakeResponse(429)
        with mock.patch.object(prov._session, 'post', return_value=resp) as post:
            ok, content, error = prov.request({})
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 3)  # 1 + CLOUD_RETRIES(2)

    def test_401_no_retry(self):
        prov = base_mod.OpenAICompatProvider()
        prov.name = 'test'
        prov.base_url = 'https://x.test/v1'
        prov.model = 'm'
        with mock.patch.object(prov._session, 'post',
                               return_value=FakeResponse(401)) as post:
            ok, content, error = prov.request({})
        self.assertFalse(ok)
        self.assertIn('401', error)
        self.assertEqual(post.call_count, 1)

    def test_reasoning_content_ignored(self):
        data = {'choices': [{'message': {'content': 'ответ',
                                         'reasoning_content': 'секрет'}}]}
        self.assertEqual(base_mod.OpenAICompatProvider._extract_content(data),
                         'ответ')


class TestGigaChat(unittest.TestCase):
    def test_not_configured_without_credentials(self):
        prov = gigachat_mod.GigaChatProvider()
        with mock.patch('jarvis.config.GIGACHAT_CLIENT_ID', ''), \
             mock.patch('jarvis.config.GIGACHAT_CLIENT_SECRET', ''):
            self.assertFalse(prov.is_configured())

    def test_oauth_and_request(self):
        prov = gigachat_mod.GigaChatProvider()
        prov._token = 'jwt-token'
        prov._token_expires_at = 1e18
        resp = FakeResponse(200, {'choices': [{'message': {'content': 'план'}}]})
        with mock.patch.object(prov._session, 'post', return_value=resp) as post:
            ok, content, error = prov.request({})
        self.assertTrue(ok)
        self.assertEqual(content, 'план')
        self.assertEqual(post.call_args[1]['headers']['Authorization'],
                         'Bearer jwt-token')

    def test_token_refresh_on_expiry(self):
        prov = gigachat_mod.GigaChatProvider()
        prov._token = None
        oauth_resp = FakeResponse(200, {'access_token': 'new-token',
                                        'expires_at': 9999999999})
        with mock.patch('jarvis.cloud.providers.gigachat.requests.post',
                        return_value=oauth_resp) as oauth, \
             mock.patch.object(prov._session, 'post',
                               return_value=FakeResponse(
                                   200, {'choices': [{'message':
                                                      {'content': 'x'}}]})) as api:
            ok, content, error = prov.request({})
        self.assertTrue(ok)
        self.assertEqual(oauth.call_count, 1)
        self.assertEqual(api.call_args[1]['headers']['Authorization'],
                         'Bearer new-token')


class TestCloudLLM(unittest.TestCase):
    def _assistant_parts(self):
        policy = policy_mod.Policy.load('policy.yaml')
        registry = Registry(policy)
        guard = PathGuard(policy)
        return policy, registry, guard

    def _fake_router(self, contents):
        """Роутер, отдающий содержимое по очереди (как провайдеры)."""
        seq = iter(contents)
        router = mock.Mock(spec=CloudRouter)

        def attempts(payload):
            for content in seq:
                if content is None:
                    yield 'p1', False, '', 'ошибка сети'
                else:
                    yield 'p1', True, content, ''
        router.attempts.side_effect = attempts
        return router

    def test_valid_plan(self):
        policy, registry, guard = self._assistant_parts()
        plan = json.dumps({'explanation': 'сделаю', 'steps': []})
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router([plan]))
        ok, result, error = llm.request_plan('привет')
        self.assertTrue(ok)
        self.assertEqual(result['steps'], [])

    def test_invalid_json_then_valid(self):
        policy, registry, guard = self._assistant_parts()
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router(['не JSON',
                                                         json.dumps(
                                                             {'explanation': 'ок',
                                                              'steps': []})]))
        ok, result, error = llm.request_plan('привет')
        self.assertTrue(ok)

    def test_rejected_plan_then_valid(self):
        policy, registry, guard = self._assistant_parts()
        bad = json.dumps({'explanation': 'х', 'steps': [
            {'tool': 'no_such_tool', 'params': {}}]})
        good = json.dumps({'explanation': 'ок', 'steps': []})
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router([bad, good]))
        ok, result, error = llm.request_plan('привет')
        self.assertTrue(ok)
        self.assertEqual(result['steps'], [])

    def test_all_providers_fail(self):
        policy, registry, guard = self._assistant_parts()
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router([None, 'не JSON']))
        ok, result, error = llm.request_plan('привет')
        self.assertFalse(ok)
        self.assertIsNone(result)

    def test_payload_has_no_model(self):
        policy, registry, guard = self._assistant_parts()
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router([]))
        payload = llm._build_payload('вопрос')
        self.assertNotIn('model', payload)
        self.assertIn('messages', payload)
        user = json.loads(payload['messages'][1]['content'])
        self.assertEqual(user['user_request'], 'вопрос')
        self.assertIn('available_tools', user)

    def test_policy_disabled(self):
        policy, registry, guard = self._assistant_parts()
        llm = llm_mod.CloudLLM(policy, registry, guard,
                               router=self._fake_router([]))
        with mock.patch('jarvis.config.CLOUD_ENABLED', False):
            ok, result, error = llm.request_plan('x')
        self.assertFalse(ok)
        self.assertIn('отключена', error)


if __name__ == '__main__':
    unittest.main()