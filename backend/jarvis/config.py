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
CLOUD_MAX_TOKENS = 1500

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