"""Глобальные настройки Jarvis.

Принцип: безопасные значения по умолчанию, всё важное переопределяется
переменными окружения (без хранения секретов в коде):

    JARVIS_DATA_DIR   — каталог данных (по умолчанию ~/.local/share/jarvis-assistant)
    JARVIS_POLICY     — путь к policy.yaml
    JARVIS_CLOUD_ENABLED — '0' полностью отключает облачную LLM
    JARVIS_ASSUME_YES — '1' автоматически подтверждает опасные действия
                        (только для автоматизации/тестов — НЕ рекомендуется)
    OPENAI_BASE_URL   — OpenAI-совместимый endpoint (по умолчанию opencode.ai/zen)
    OPENAI_MODEL      — модель облака
    OPENAI_API_KEY    — ключ (если нужен; бесплатный шлюз opencode.ai/zen не требует)
"""

import os

# ============================== ПУТИ ======================================

HOME = os.path.expanduser('~')

DATA_DIR = os.path.expanduser(
    os.environ.get('JARVIS_DATA_DIR', '~/.local/share/jarvis-assistant'))

# policy.yaml ищется: 1) явный путь из окружения; 2) в каталоге проекта
# (backend/policy.yaml при разработке); 3) в установочном каталоге.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.environ.get('JARVIS_POLICY') or next(
    (p for p in (os.path.join(_BACKEND_DIR, 'policy.yaml'),
                 os.path.join(DATA_DIR, 'policy.yaml'))
     if os.path.isfile(p)),
    os.path.join(_BACKEND_DIR, 'policy.yaml'),
)

# Модель локального классификатора намерений (NumPy MLP, сохраняется в npz)
MODEL_PATH = os.path.join(DATA_DIR, 'models', 'intent_mlp.npz')

# База SQLite FTS5 для индекса файлов
INDEX_DB_PATH = os.path.join(DATA_DIR, 'index.db')

# База памяти ассистента: реплики диалога, факты, история действий
MEMORY_DB_PATH = os.path.join(DATA_DIR, 'memory.db')

# Каталог JSONL-логов (значение по умолчанию; policy.yaml может переопределить)
LOG_DIR = os.path.expanduser(
    os.environ.get('JARVIS_LOG_DIR', '~/.local/share/jarvis-assistant/logs'))

# ============================== ЛОКАЛЬНАЯ МОДЕЛЬ ===========================

# Порог уверенности: ниже — запрос уходит в облачную LLM.
# Низкий порог = больше автономии локальной модели, высокий = чаще облако.
CLASSIFIER_CONFIDENCE_THRESHOLD = 0.45

# Размер векторного признакового пространства (char-граммы, хэшированные)
FEATURE_DIM = 8192

# ============================== ОБЛАЧНАЯ LLM ==============================

# OpenAI-совместимый шлюз. По умолчанию — бесплатный DeepSeek V4 Flash Free
# (opencode.ai/zen/v1, без ключа и регистрации).
CLOUD_ENABLED = os.environ.get('JARVIS_CLOUD_ENABLED', '1') != '0'
CLOUD_BASE_URL = os.environ.get('OPENAI_BASE_URL', 'https://opencode.ai/zen/v1')
CLOUD_MODEL = os.environ.get('OPENAI_MODEL', 'deepseek-v4-flash-free')
CLOUD_API_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
CLOUD_TIMEOUT_SEC = 90
CLOUD_RETRIES = 2
CLOUD_RETRY_DELAY_SEC = 2.0

# ВСЕ бесплатные модели шлюза opencode.ai/zen (снимаются по /v1/models):
# цепочка облака пробует их по очереди, пока одна не ответит. Порядок —
# по предпочтению (первая — самая быстрая). Можно переопределить
# OPENCODE_FREE_MODELS='м1,м2,...' (пусто — остаётся одна CLOUD_MODEL).
OPENCODE_FREE_MODELS = [
    m.strip() for m in os.environ.get(
        'OPENCODE_FREE_MODELS',
        'deepseek-v4-flash-free,'
        'mimo-v2.5-free,'
        'hy3-free,'
        'nemotron-3-ultra-free,'
        'nemotron-3.5-lightning-free,'
        'laguna-s-2.1-free').split(',') if m.strip()
]
CLOUD_MAX_TOKENS = 1500

# ============================== ЦЕПОЧКА ПРОВАЙДЕРОВ (ЧАСТЬ 5) ================
# Порядок попыток: локальная Ollama -> DeepSeek -> GigaChat -> YandexGPT ->
# opencode.ai (бесплатный шлюз без ключа). Провайдер без ключа/токена
# пропускается. Секреты — только из окружения.
# Ограничения: RATE_LIMIT_PER_MINUTE запросов на провайдера в минуту;
# после RATE_LIMIT_COOLDOWN_FAILURES подряд неудач провайдер уходит
# в RATE_LIMIT_COOLDOWN_SEC на 60 секунд (circuit breaker).

OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder:1.5b')
OLLAMA_TIMEOUT_SEC = 30

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '').strip()
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL',
                                   'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')

GIGACHAT_CLIENT_ID = os.environ.get('GIGACHAT_CLIENT_ID', '').strip()
GIGACHAT_CLIENT_SECRET = os.environ.get('GIGACHAT_CLIENT_SECRET', '').strip()
GIGACHAT_AUTH_URL = ('https://ngw.devices.sberbank.ru:9443/api/v2/oauth')
GIGACHAT_API_URL = ('https://gigachat.devices.sberbank.ru/api/v1/chat/completions')
GIGACHAT_MODEL = 'GigaChat'

YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '').strip()
YANDEX_API_URL = 'https://llm.api.cloud.yandex.net/v1/chat/completions'
YANDEX_MODEL = os.environ.get('YANDEX_MODEL', 'yandexgpt-lite')

RATE_LIMIT_PER_MINUTE = int(os.environ.get('JARVIS_RATE_LIMIT_PER_MINUTE', '12'))
RATE_LIMIT_COOLDOWN_SEC = int(os.environ.get('JARVIS_RATE_COOLDOWN_SEC', '60'))
RATE_LIMIT_COOLDOWN_FAILURES = 2

# ============================== ВЕБ-ИНТЕРФЕЙС (ЧАСТЬ 6) ======================
# Панель слушает ТОЛЬКО localhost. JARVIS_WEB_TOKEN — опциональный токен:
# если задан, все запросы должны нести заголовок Authorization: Bearer <token>
# (защита от других локальных процессов в браузере).

WEB_HOST = os.environ.get('JARVIS_WEB_HOST', '127.0.0.1')
WEB_PORT = int(os.environ.get('JARVIS_WEB_PORT', '8747'))
WEB_TOKEN = os.environ.get('JARVIS_WEB_TOKEN', '').strip()
WEB_LOG_LINES_MAX = 500  # максимум строк лога за один запрос

# ============================== ГОЛОС ======================================
# Голосовой режим демона (слово-активатор «Ева» -> STT -> пайплайн -> TTS).
# Все компоненты опциональны: если нет моделей/микрофона, демон работает
# текстом (JARVIS_VOICE=0 полностью отключает голос).

VOICE_ENABLED = os.environ.get('JARVIS_VOICE', '1') != '0'

SAMPLE_RATE = 16000          # аудио микрофона (16 кГц для Vosk/Whisper)
BLOCK_SIZE = 8000            # 0.5 с при 16 кГц
AUDIO_TAIL_SECONDS = 2.0     # «хвост» аудио до активации (кольцевой буфер)
MIC_DEVICE = None            # None = микрофон по умолчанию (или индекс sounddevice)
MIC_GAIN_TARGET_RMS = 1000   # до какого уровня RMS подтягивать тихие блоки
MIC_GAIN_MAX = 6.0           # максимум усиления (не тянем фоновый шум)

SILENCE_HANG_SECONDS = 1.5   # тишина такой длины завершает запись команды
MAX_COMMAND_SECONDS = 12     # жёсткий предел длительности команды
RECORD_RETRIES = 2           # переспросов, если команда не распознана

STARTUP_DELAY_SECONDS = 10   # пауза после логина перед загрузкой моделей
ACTIVATION_MODE = 'voice'    # voice / hotkey / both (меняется по D-Bus)
WAKE_WORDS = ('ева', 'эва')  # слово-активатор

VOSK_MODEL_PATH = os.path.join(DATA_DIR, 'models', 'vosk-model-small-ru')

# Нейросетевой детектор «Ева» (WakeNet ONNX int8, обученная модель):
# работает вместо текстового матчинга Vosk в цикле ожидания. Если
# модели/пакета нет — движок автоматически возвращается к Vosk.
WAKE_MODEL_PATH = os.path.join(DATA_DIR, 'checkpoints', 'wakeword.onnx')
WAKE_STATS_PATH = os.path.join(DATA_DIR, 'checkpoints', 'stats.npz')
WAKE_NN_THRESHOLD = 0.95   # P(«Ева»); +2 подряд окна — temporal smoothing

# faster-whisper (STT): грузится лениво при первой команде
WHISPER_MODEL_SIZE = 'small'       # tiny/base/small/medium
WHISPER_DEVICE = 'cpu'
WHISPER_COMPUTE_TYPE = 'int8'      # хорошо для CPU
WHISPER_CPU_THREADS = max(2, min(4, (os.cpu_count() or 4) - 1))
WHISPER_BEAM_SIZE = 5              # больше beam = точнее, но медленнее
WHISPER_UNLOAD_IDLE_SECONDS = 600  # выгрузка модели при простое (освобождает ОЗУ)
WHISPER_INITIAL_PROMPT = (
    'Ева браузер терминал калькулятор файлы настройки почта погода время '
    'музыка видео громкость тише громче звук свет яркость скриншот окно '
    'приложение открой закрой покажи найди создай удали перезагрузи '
    'выключи валюты доллар курс'
)

# TTS: порядок edge-tts (онлайн, «Светлана») -> RHVoice (Elena) -> Piper (irina)
EDGE_TTS_VOICE = 'ru-RU-SvetlanaNeural'
EDGE_TTS_RATE = '+15%'
RHVOICE_VOICE = 'Elena'
RHVOICE_DATA_PATH = os.path.join(DATA_DIR, 'models', 'rhvoice')  # без sudo
RHVOICE_RATE = 1.25
PIPER_VOICE_MODEL = os.path.join(
    DATA_DIR, 'models', 'piper', 'ru_RU-irina-medium.onnx')
PIPER_LENGTH_SCALE = 0.9

# Непрерывный диалог без слова-активатора после ответа (как у Алисы)
DIALOGUE_MODE_ENABLED = True
DIALOGUE_TIMEOUT_SECONDS = 5       # тишина такой длины завершает диалог
DIALOGUE_ECHO_GUARD_SECONDS = 1.0  # пауза после озвучки (защита от эха)
DIALOGUE_MIN_SPEECH_SECONDS = 0.6  # минимальная длина реплики

WAKE_DEBUG = os.environ.get('JARVIS_WAKE_DEBUG', '0') == '1'

# ============================== ПОИСК ======================================

SEARCH_MAX_RESULTS = 20      # максимум результатов для fd/rg за один вызов
SEARCH_TEXT_MAX_FILESIZE = '10M'

# ============================== ПРОЧЕЕ =====================================

# Максимальный размер текстового файла, который читает индексатор (байт)
INDEX_MAX_FILE_BYTES = 10 * 1024 * 1024

# Таймауты инструментов (сек)
TOOL_TIMEOUT_SEC = 15

# ============================== НОВЫЕ ИНТЕНТЫ (ЧАСТЬ 2) ====================
# Таймеры/будильники ставятся через systemd-run (переживают перезапуск
# демона); напоминания хранятся в своей БД — их разбирает планировщик
# (jarvis/proactive/scheduler.py, ЧАСТЬ 3) каждую секунду.

REMINDERS_DB_PATH = os.path.join(DATA_DIR, 'reminders.db')

# История буфера обмена (своя: xclip не хранит историю)
CLIPBOARD_HISTORY_DB_PATH = os.path.join(DATA_DIR, 'clipboard.db')

# Офлайн-кэш курсов валют (обновляется раз в сутки; при отсутствии сети
# используется последний сохранённый курс)
CURRENCY_CACHE_PATH = os.path.join(DATA_DIR, 'currency.json')
CURRENCY_CACHE_MAX_AGE_SEC = 24 * 3600
CURRENCY_API_URL = 'https://open.er-api.com/v6/latest/USD'
CURRENCY_API_TIMEOUT_SEC = 8

# Календарь: локальный .ics (кэш курсов/новостей — в памяти процесса)
CALENDAR_ICS_PATH = os.path.expanduser(
    os.environ.get('JARVIS_CALENDAR_ICS', os.path.join(DATA_DIR, 'calendar.ics')))

# Почта (send_email): все настройки — из окружения, секретов в коде нет.
# JARVIS_SMTP_TO — адрес получателя по умолчанию.
SMTP_HOST = os.environ.get('JARVIS_SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('JARVIS_SMTP_PORT', '465'))
SMTP_USER = os.environ.get('JARVIS_SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('JARVIS_SMTP_PASSWORD', '')
SMTP_DEFAULT_TO = os.environ.get('JARVIS_SMTP_TO', '')

# Перевод текста: бесплатный API без ключа (MyMemory). Секретов не требует.
TRANSLATE_API_URL = ('https://api.mymemory.translated.net/get'
                     '?q={query}&langpair={src}|{dst}')
TRANSLATE_API_TIMEOUT_SEC = 10

# Погода: wttr.in без ключа
WEATHER_API_URL = 'https://wttr.in/{city}?format=%C+%t+%w&lang=ru'
WEATHER_API_TIMEOUT_SEC = 8

# RSS-новости: проверяются по очереди до первого успеха
NEWS_FEEDS = [
    'https://lenta.ru/rss/',
    'https://www.securitylab.ru/_services/export/rss/',
]
NEWS_MAX_ITEMS = 5

# ============================== ПРОАКТИВ (ЧАСТЬ 3) ==========================
# Планировщик (jarvis/proactive/scheduler.py) живёт в демоне: каждую секунду
# проверяет напоминания, каждые PROACTIVE_TRIGGER_INTERVAL_SEC — триггеры.
# Напоминания переживают перезапуск демона: просроченные срабатывают сразу
# при старте.

PROACTIVE_POLL_INTERVAL_SEC = 1.0
PROACTIVE_TRIGGER_INTERVAL_SEC = 10.0

# Триггеры по умолчанию. Формат:
#   {'type': 'process', 'name': 'code',   'title': '...', 'text': '...'}
#   {'type': 'file',    'path': '/tmp/x', 'title': '...', 'text': '...'}
# Срабатывают один раз при появлении условия и снова — после его исчезновения.
# Можно переопределить целиком через JARVIS_PROACTIVE_TRIGGERS (JSON-список).
PROACTIVE_TRIGGERS = [
    {'type': 'process', 'name': 'code',
     'title': 'VS Code открыт',
     'text': 'Могу продолжить работу над проектом.'},
    {'type': 'process', 'name': 'firefox',
     'title': 'Браузер открыт',
     'text': 'Могу найти нужную страницу, если скажете.'},
]


def _load_proactive_triggers():
    """Триггеры из окружения (JSON), если заданы; иначе значения по умолчанию."""
    raw = os.environ.get('JARVIS_PROACTIVE_TRIGGERS', '').strip()
    if not raw:
        return PROACTIVE_TRIGGERS
    try:
        import json
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            return parsed
    except ValueError:
        pass
    return PROACTIVE_TRIGGERS


PROACTIVE_TRIGGERS = _load_proactive_triggers()