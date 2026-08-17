"""Точка входа: CLI, D-Bus демон, индексация, обучение.

    python -m jarvis.main cli [«фраза»]   — интерактивная консоль (по умолчанию)
    python -m jarvis.main daemon          — D-Bus демон для расширения GNOME
    python -m jarvis.main index           — пересобрать индекс файлов
    python -m jarvis.main train           — переобучить локальную модель
    python -m jarvis.main status          — проверка окружения
"""

import argparse
import os
import sys

from jarvis import config
from jarvis import logger
from jarvis import policy as policy_mod


def load_policy():
    return policy_mod.Policy.load(config.POLICY_PATH)


# ============================== CLI ========================================


def run_cli(args):
    """Интерактивная консоль (или разовый запрос из аргумента)."""
    from jarvis.pipeline import make_assistant

    policy = load_policy()
    assistant = make_assistant(policy, prompt_fn=_cli_confirm)

    def _print_result(result):
        intent = result['intent']
        conf = result.get('confidence', 0)
        route = result.get('route', '')
        print(f'\n[{intent} · {conf:.0%} · {route}]')
        print(result['response'])

    if args.command:
        _print_result(assistant.process(args.command))
        return 0

    print('Jarvis — локальный ассистент. Введите запрос.')
    print('Команды: /quit — выход, /index — пересобрать индекс, '
          '/train — переобучить модель, /status — окружение.')
    while True:
        try:
            line = input('\njarvis> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nДо свидания!')
            return 0
        if not line:
            continue
        if line.startswith('/'):
            if line in ('/quit', '/exit', '/q'):
                return 0
            if line == '/index':
                _run_index()
                continue
            if line == '/train':
                _run_train()
                continue
            if line == '/status':
                _run_status()
                continue
            print(f'Неизвестная команда: {line}')
            continue
        try:
            _print_result(assistant.process(line))
        except Exception as exc:  # noqa: BLE001 — CLI не должен падать
            print(f'Ошибка обработки: {exc}')


def _cli_confirm(question):
    """Подтверждение опасных действий в CLI."""
    try:
        return input(question)
    except (EOFError, KeyboardInterrupt):
        return 'n'


# ============================== ИНДЕКС / ОБУЧЕНИЕ ==========================


def _run_index():
    policy = load_policy()
    from jarvis.indexer.indexer import FileIndexer
    indexer = FileIndexer(config.INDEX_DB_PATH, policy)
    count = indexer.build()
    print(f'Индекс пересобран: {count} файлов -> {config.INDEX_DB_PATH}')


def _run_train():
    from jarvis.ml.classifier import IntentClassifier
    classifier = IntentClassifier(config.MODEL_PATH, config.FEATURE_DIM,
                                  config.CLASSIFIER_CONFIDENCE_THRESHOLD)
    val_acc, samples = classifier.train()
    print(f'Модель обучена: {samples} примеров, '
          f'точность на валидации {val_acc:.1%}')
    print(f'Сохранено: {config.MODEL_PATH}')


def _run_status():
    policy = load_policy()
    print(f'Политика: {policy.source}')
    print(f'  allowed_roots: {policy.allowed_roots}')
    print(f'  denied: {len(policy.denied_paths)} каталогов в denylist')
    print(f'  index_roots: {policy.index_roots}')
    from jarvis.cloud.llm import environment_snapshot
    import json
    print('Окружение:')
    print(json.dumps(environment_snapshot(), ensure_ascii=False, indent=2))


# ============================== ДЕМОН ======================================


def run_daemon(args):
    """D-Bus демон. Голосовой движок (слово-активатор «Ева», STT, TTS)
    включается при наличии звука/моделей; подтверждения опасных действий
    спрашиваются голосом. Текстовые команды без голосового цикла получают
    безопасный отказ (Confirmator без голоса отклоняет с объяснением)."""
    from jarvis.pipeline import make_assistant
    from jarvis.dbus.service import run_dbus_service

    policy = load_policy()

    engine = None
    if config.VOICE_ENABLED:
        try:
            from jarvis.voice.engine import VoiceEngine
            engine = VoiceEngine()
        except Exception as exc:
            print(f'[daemon] голосовой режим недоступен: {exc}')

    def daemon_prompt_fn(question):
        """Подтверждения: голосом (если движок жив), иначе безопасный отказ."""
        if engine is not None and engine.available:
            return engine.confirm_voice(question)
        return 'n'

    assistant = make_assistant(policy, prompt_fn=daemon_prompt_fn,
                               auto_yes=False)
    print(f'Jarvis daemon: политика {policy.source}, '
          f'облако {"вкл" if assistant.cloud else "выкл"}, '
          f'голос {"вкл" if engine is not None else "выкл"}')

    from jarvis.proactive.scheduler import ProactiveScheduler
    scheduler = ProactiveScheduler()
    print(f'[daemon] проактивный планировщик запущен '
          f'({len(scheduler.pending())} активных напоминаний, '
          f'{len(config.PROACTIVE_TRIGGERS)} триггеров)')

    run_dbus_service(assistant, policy, engine=engine, scheduler=scheduler)


# ============================== ВЕБ-ПАНЕЛЬ ==================================


def run_web(args):
    """Веб-панель (FastAPI, localhost): чат, напоминания, настройки, логи.

    Опасные действия подтверждаются так же, как в текстовом режиме без
    голоса: Confirmator без интерактивного промпта отклоняет их с
    объяснением. Планировщик напоминаний запускается вместе с панелью.
    """
    from jarvis.pipeline import make_assistant
    from jarvis.proactive.scheduler import ProactiveScheduler
    from jarvis.web.app import create_app

    policy = load_policy()
    assistant = make_assistant(policy, prompt_fn=_cli_confirm,
                               auto_yes=False)
    scheduler = ProactiveScheduler()
    app = create_app(assistant, policy, scheduler=scheduler)

    import uvicorn
    print(f'Jarvis web: http://{config.WEB_HOST}:{config.WEB_PORT} '
          f'(токен {"вкл" if config.WEB_TOKEN else "выкл"})')
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT,
                log_level='warning')


# ============================== MAIN =======================================


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='jarvis',
        description='Jarvis — безопасный локальный ассистент для Linux')
    sub = parser.add_subparsers(dest='mode')

    p_cli = sub.add_parser('cli', help='интерактивная консоль (по умолчанию)')
    p_cli.add_argument('command', nargs='?', help='разовый запрос')

    sub.add_parser('daemon', help='D-Bus демон для расширения GNOME')
    sub.add_parser('web', help='веб-панель (FastAPI, localhost)')
    sub.add_parser('index', help='пересобрать индекс файлов (FTS5)')
    sub.add_parser('train', help='переобучить локальную модель')
    sub.add_parser('status', help='проверить окружение и политику')

    args = parser.parse_args(argv)
    mode = args.mode or 'cli'

    # гарантируем каталоги данных при любом режиме
    os.makedirs(config.DATA_DIR, exist_ok=True)

    if mode == 'daemon':
        return run_daemon(args)
    if mode == 'web':
        return run_web(args)
    if mode == 'index':
        _run_index()
        return 0
    if mode == 'train':
        _run_train()
        return 0
    if mode == 'status':
        _run_status()
        return 0
    return run_cli(args)


if __name__ == '__main__':
    sys.exit(main())