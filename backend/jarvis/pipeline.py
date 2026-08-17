"""Полный цикл обработки запроса: «текст -> ответ».

Маршрутизация:
    1. локальный классификатор определяет intent + confidence + slots;
    2. если intent == 'chat' ИЛИ уверенность ниже порога — запрос уходит
       в облачную LLM, которая возвращает JSON-план (валидируется,
       затем исполняется Executor'ом; если шагов нет — explanation
       становится ответом);
    3. иначе намерение исполняется локально (Executor.run_intent);
    4. каждое событие логируется в JSONL.

Ни одна ветка не исполняет сырые команды: всё — через реестр
инструментов и политику.
"""

from jarvis import config
from jarvis import intents
from jarvis import logger
from jarvis.plan import plan_summary


class Assistant:
    """Сборка всех компонентов в один интерфейс."""

    def __init__(self, policy, classifier, executor, cloud=None,
                 indexer=None, log=None):
        self.policy = policy
        self.classifier = classifier
        self.executor = executor
        self.cloud = cloud
        self.indexer = indexer
        self.log = log or logger.get_logger()
        self.last_intent = None

    # --------------------------- обработка ----------------------------------

    def process(self, text):
        """Запрос -> словарь {response, intent, confidence, route}."""
        text = (text or '').strip()
        if not text:
            return {'response': 'Скажите, что сделать.', 'intent': 'chat',
                    'confidence': 0.0, 'route': 'empty'}

        pred = self.classifier.predict(text)
        self.last_intent = pred['intent']
        self.log.event('request', text=text[:500], **pred)

        # Локальный маршрут
        if (pred['intent'] != 'chat'
                and pred['confidence'] >= self.classifier.confidence_threshold):
            ok, message, data = self.executor.run_intent(pred['intent'],
                                                         pred['slots'])
            return {'response': message or 'Готово.',
                    'intent': pred['intent'],
                    'confidence': pred['confidence'],
                    'route': 'local', 'ok': ok, 'data': data}

        # Облачный маршрут (сложный запрос или низкая уверенность)
        return self._cloud_route(text, pred)

    # --------------------------- облако --------------------------------------

    def _cloud_route(self, text, pred):
        if self.cloud is None:
            return {'response': ('Этот запрос сложнее моих локальных навыков, '
                                 'а облачная модель сейчас недоступна. '
                                 'Попробуйте переформулировать проще.'),
                    'intent': 'chat', 'confidence': pred['confidence'],
                    'route': 'cloud_unavailable'}

        ok, plan, error = self.cloud.request_plan(text)
        if not ok:
            return {'response': (f'Не удалось получить план от облачной '
                                 f'модели: {error}'),
                    'intent': pred['intent'],
                    'confidence': pred['confidence'],
                    'route': 'cloud_error'}

        if not plan['steps']:
            return {'response': plan['explanation'] or 'Готово.',
                    'intent': pred['intent'],
                    'confidence': pred['confidence'],
                    'route': 'cloud_answer'}

        self.log.event('plan_executing', summary=plan_summary(plan))
        ok, message, results = self.executor.run_plan(plan)
        response = message
        if ok and plan.get('explanation'):
            response = plan['explanation'] + '\n' + message
        return {'response': response, 'intent': pred['intent'],
                'confidence': pred['confidence'],
                'route': 'cloud_plan', 'ok': ok, 'steps': results}


def make_assistant(policy, prompt_fn=None, auto_yes=False, log=None,
                   with_indexer=False):
    """Фабрика: собирает Assistant из готовых компонентов.

    prompt_fn — функция вопроса для подтверждений (CLI: input()).
    auto_yes — JARVIS_ASSUME_YES=1 (автоматическое подтверждение).
    """
    from jarvis.ml.classifier import IntentClassifier
    from jarvis.tools.registry import Registry
    from jarvis.security import PathGuard, Confirmator
    from jarvis.executor import Executor

    log = log or logger.get_logger()
    classifier = IntentClassifier(config.MODEL_PATH,
                                  config.FEATURE_DIM,
                                  config.CLASSIFIER_CONFIDENCE_THRESHOLD)
    registry = Registry(policy)
    guard = PathGuard(policy)
    confirmator = Confirmator(policy, prompt_fn=prompt_fn, auto_yes=auto_yes)
    executor = Executor(policy, registry, guard, confirmator, log=log)

    cloud = None
    if policy.cloud.get('enabled', True) and config.CLOUD_ENABLED:
        from jarvis.cloud.llm import CloudLLM
        cloud = CloudLLM(policy, registry, guard, log=log)

    indexer = None
    if with_indexer:
        from jarvis.indexer.indexer import FileIndexer
        indexer = FileIndexer(config.INDEX_DB_PATH, policy, log=log)

    return Assistant(policy, classifier, executor, cloud=cloud,
                     indexer=indexer, log=log)