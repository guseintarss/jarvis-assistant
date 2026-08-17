"""Реестр намерений (intents) ассистента.

Каждое намерение описывает:
    name        — идентификатор (совпадает с именем инструмента);
    description — для человека и для промпта облачной LLM;
    risk        — низкий/средний/высокий (политика подтверждений);
    slots       — регулярные выражения для извлечения параметров.

Классификатор (ml/) определяет ТОЛЬКО намерение и уверенность; слоты
извлекаются детерминированно регэкспами из этого файла — так параметры
не зависят от капризов нейросети.
"""

import re

# ============================== УРОВНИ РИСКА ===============================

RISK_LOW = 'low'
RISK_MEDIUM = 'medium'
RISK_HIGH = 'high'

RISK_LABELS = {
    RISK_LOW: 'низкий',
    RISK_MEDIUM: 'средний',
    RISK_HIGH: 'высокий',
}

# Служебные слова, которые срезаются с конца извлечённого слота
_TRAILING_STOPWORDS = ('пожалуйста', 'пожалуйста.', 'сейчас', 'прямо сейчас',
                       'мне', 'на', 'уже', 'быстрее', 'скорее', 'плиз',
                       'пж', 'поскорее', 'просто')


def _clean(value):
    """Чистит слот: убирает пунктуацию, служебные хвосты, схлопывает пробелы."""
    if not value:
        return ''
    value = re.sub(r'[«»"\'.,;:!?()]+$', '', value.strip())
    words = value.split()
    while words and words[-1].lower() in _TRAILING_STOPWORDS:
        words.pop()
    return ' '.join(words).strip()


def _percent(text):
    """Число процентов из фразы ('на 10 процентов', 'до 30%'), иначе ''."""
    m = re.search(r'(\d{1,3})\s*(?:%|процент|процентов|процента)', text)
    return m.group(1) if m else ''


# ============================== ОПИСАНИЯ ===================================

# Каждый слот: {имя: (pattern, группа, clean: bool)}
# Обязательные слоты, которые инструмент сможет использовать как параметры.
SLOTS = {
    'open_app': {
        'app': (r'(?:открой|откройте|открыть|запусти|запустите|запустить|'
                r'включи|включить|покажи|покажите)\s+(?:мне\s+|пожалуйста\s+)?'
                r'(?:приложение\s+|приложени[ея]\s+|программ[уы]\s+|программу\s+)?'
                r'([а-яёa-z0-9_.+\- ]{1,40})', 1, True),
    },
    'open_file': {
        'path': (r'(?:открой|откройте|открыть|покажи|покажите)\s+(?:мне\s+)?'
                 r'(?:файл\s+|файлы\s+)?([^\s,;.!?]+(?:\.[\w\-]{1,10})?)', 1, True),
    },
    'open_url': {
        'url': (r'((?:https?://|www\.)[^\s,;.!?]+|[\w\-]+\.'
                r'(?:ru|com|org|net|io|dev|me|info|xyz|online|site|tech|space'
                r'|pro|top|biz|tv|wiki|moe|cc|app|cloud|su|ua|by|kz|am)\b)', 1, True),
    },
    'search_files': {
        'query': (r'(?:найди|найти|поищи|найду|где\s+(?:лежит|находится|есть|'
                  r'хранится))\s+(?:мне\s+)?(?:файл\s+|файлы\s+|документ\s+|'
                  r'документы\s+|по имени\s+)?(.+)', 1, True),
    },
    'search_text': {
        'query': (r'(?:найди|поищи|найти|где\s+(?:встречается|упоминается|'
                  r'написано|ищется|есть))\s+(?:мне\s+)?(?:текст\s+|слово\s+|'
                  r'фразу\s+|строку\s+|упоминание\s+|в\s+файлах\s+)?(.+)', 1, True),
    },
    'volume_up': {
        'step': (r'(\d{1,3})\s*(?:%|процент|процентов|процента)', 1, True),
    },
    'volume_down': {
        'step': (r'(\d{1,3})\s*(?:%|процент|процентов|процента)', 1, True),
    },
    'brightness_up': {
        'step': (r'(\d{1,3})\s*(?:%|процент|процентов|процента)', 1, True),
    },
    'brightness_down': {
        'step': (r'(\d{1,3})\s*(?:%|процент|процентов|процента)', 1, True),
    },
    'notify': {
        'message': (r'(?:напомни|напомнить|уведоми|уведомь|сообщи|покажи\s+'
                    r'уведомление|напоминание)\s+(?:мне\s+)?(?:что\s+|про\s+|'
                    r'о\s+)?(.+)', 1, True),
    },
    'media_play_pause': {
        'action': (r'(?:следующ|дальше|previous|next|next\b|предыдущ|назад'
                   r'|останови|остановить|пауза|play|stop|pause)', 0, True),
    },
    'lock_screen': {},
    'screenshot': {},
    'move_to_trash': {
        'path': (r'(?:удали|удалить|выбрось|выбросить|в\s+корзину|убери|'
                 r'удалите)\s+(?:мне\s+)?(?:файл\s+|файлы\s+)?'
                 r'([^\s,;.!?]+(?:\.[\w\-]{1,10})?)', 1, True),
    },
    'chat': {},
    # -------------------------- время ---------------------------------------
    'set_timer': {
        'duration': (r'((?:\d+\s*(?:час\w*|минут\w*|секунд\w*)\s*){1,3})',
                     1, True),
    },
    'set_alarm': {
        'time': (r'(\d{1,2})\s*[:\.]\s*(\d{2})', 0, True),
        'hour': (r'(\d{1,2})\s*(?:часов?\s*)?(?:утра|вечера)', 1, True),
    },
    'set_reminder': {
        'time': (r'(\d{1,2})\s*[:\.]\s*(\d{2})', 0, True),
        'day': (r'(сегодня|завтра|послезавтра|в\s+понедельник|в\s+вторник|'
                r'в\s+среду|в\s+четверг|в\s+пятницу|в\s+субботу|в\s+'
                r'воскресенье)', 1, True),
        'text': (r'(?:напомни|напомнить)\s+(?:мне\s+)?(?:сегодня\s+|'
                 r'завтра\s+|послезавтра\s+)?(?:в\s+\d{1,2}\s*[:\.]\s*\d{2}'
                 r'\s+)?(?:про\s+|что\s+|о\s+)?(.+)$', 1, True),
    },
    'check_time': {},
    'check_date': {},
    'list_reminders': {},
    'cancel_reminder': {
        'target': (r'(все|всё|последн\w+|\d{1,3})', 1, True),
    },
    # -------------------------- окна ----------------------------------------
    'minimize_window': {
        'window': (r'(?:сверни|свернуть|минимизируй)\s+(?:окно\s+)?'
                   r'(?:приложения\s+)?([^\s,;.!?]{1,40})', 1, True),
    },
    'maximize_window': {
        'window': (r'(?:разверни|развернуть|максимизируй)\s+(?:окно\s+)?'
                   r'(?:приложения\s+)?([^\s,;.!?]{1,40})', 1, True),
    },
    'close_window': {
        'window': (r'(?:закрой|закрыть)\s+(?:окно\s+)?(?:приложения\s+)?'
                   r'([^\s,;.!?]{1,40})', 1, True),
    },
    'list_windows': {},
    'switch_window': {
        'window': (r'(?:переключись?\s+на|переключить\s+на|перейди\s+в|'
                   r'перейти\s+в)\s+(?:окно\s+)?([^\s,;.!?]{1,40})', 1, True),
    },
    'switch_workspace': {
        'number': (r'(?:на\s+)?(\d{1,2})', 1, True),
    },
    # -------------------------- текст ---------------------------------------
    'count_words': {
        'text': (r'(?:посчитай|сосчитай|подсчитай)\s+(?:количество\s+)?'
                 r'(?:слов\s+)?(?:в\s+)?(.+)$', 1, True),
    },
    'change_case': {
        'case': (r'(верхн\w+|заглавн\w+|прописн\w+|нижн\w+|строчн\w+)',
                 1, True),
        'text': (r'(?:сделай|сделать|напиши|написать|переведи)\s+'
                 r'(?:текст\s+|текста\s+)?(.+?)\s+(?:в\s+)?(?:верхн\w+|'
                 r'заглавн\w+|прописн\w+|нижн\w+|строчн\w+)'
                 r'(?:\s+букв\w*|\s+регистр[еа]?)?$', 1, True),
    },
    'translate_text': {
        'lang': (r'(?:на\s+)?(английск\w+|русск\w+|немецк\w+|испанск\w+|'
                 r'французск\w+|итальянск\w+)', 1, True),
        'text': (r'(?:переведи|перевести|переведите)\s+(?:фразу\s+|'
                 r'текст\s+)?(.+?)(?:\s+на\s+(?:английск\w+|русск\w+|'
                 r'немецк\w+|испанск\w+|французск\w+|итальянск\w+)\s*)$',
                 1, True),
    },
    # -------------------------- система -------------------------------------
    'system_info': {},
    'check_disk': {},
    'check_battery': {},
    'check_network': {},
    'list_processes': {
        'n': (r'(\d{1,3})', 1, True),
    },
    'kill_process': {
        'pid': (r'\b(\d{1,7})\b', 1, True),
    },
    # -------------------------- калькулятор ---------------------------------
    'calculate': {
        'expression': (r'(?:сколько\s+(?:будет|же\s+будет|составит)|'
                       r'посчитай|подсчитай|вычисли|посчитайте|посчитать|'
                       r'вычислить)\s+(?:мне\s+)?(?:будет\s+)?(.+)$',
                       1, True),
    },
    'convert_currency': {
        'amount': (r'(\d+(?:[.,]\d+)?)', 1, True),
        'from': (r'(доллар\w*|евро|рубл\w*|фунт\w*|иен\w*|юан\w*|франк\w*|'
                 r'тенг\w*|гривн\w*|лир\w*|крон\w*)', 1, True),
        'to': (r'в\s+(доллар\w*|евро|рубл\w*|фунт\w*|иен\w*|юан\w*|'
               r'франк\w*|тенг\w*|гривн\w*|лир\w*|крон\w*)', 1, True),
    },
    'convert_units': {
        'amount': (r'(\d+(?:[.,]\d+)?)', 1, True),
        'from': (r'(километр\w*|мет\w*|сантиметр\w*|миллиметр\w*|килограмм\w*'
                 r'|грамм\w*|тонн\w*|литр\w*|миллилитр\w*|час\w*|минут\w*|'
                 r'секунд\w*|килобайт\w*|мегабайт\w*|гигабайт\w*|байт\w*|'
                 r'градус\w*\s+цельсия|градус\w*\s+фаренгейта)', 1, True),
        'to': (r'в\s+(километр\w*|мет\w*|сантиметр\w*|миллиметр\w*|'
               r'килограмм\w*|грамм\w*|тонн\w*|литр\w*|миллилитр\w*|'
               r'час\w*|минут\w*|секунд\w*|килобайт\w*|мегабайт\w*|'
               r'гигабайт\w*|байт\w*|градус\w*\s+цельсия|'
               r'градус\w*\s+фаренгейта)', 1, True),
    },
    # -------------------------- буфер обмена --------------------------------
    'clipboard_copy': {
        'text': (r'(?:скопируй|скопировать|копируй)\s+(?:в\s+буфер\s+'
                 r'обмена\s+)?(?:текст\s+)?(.+)$', 1, True),
    },
    'clipboard_paste': {},
    'clipboard_history': {},
    # -------------------------- внешние сервисы -----------------------------
    'check_weather': {
        'city': (r'(?:в\s+|для\s+|на\s+)?([а-яёa-z\- ]{2,30})$', 1, True),
    },
    'check_news': {},
    'send_email': {
        'to': (r'([\w.+\-]+@[\w\-]+\.[\w.]+)', 1, True),
        'text': (r'(?:напиши|отправь|пошли)\s+(?:письмо\s+|письма\s+)?'
                 r'.*?(?:с\s+текстом\s+|текст\s+)?(.+)$', 1, True),
    },
    'check_calendar': {},
}


class Intent:
    """Описание одного намерения."""

    def __init__(self, name, description, risk, fallback=False):
        self.name = name
        self.description = description
        self.risk = risk
        self.fallback = fallback          # chat — фолбэк, исполняется облаком
        self.slots = SLOTS.get(name, {})

    def extract_slots(self, text):
        """Извлекает слоты детерминированными регэкспами."""
        found = {}
        for slot_name, (pattern, group, clean) in self.slots.items():
            m = re.search(pattern, text, re.IGNORECASE)
            if m and group <= len(m.groups()):
                value = m.group(group)
                found[slot_name] = _clean(value) if clean else value
        return found


# ============================== РЕЕСТР =====================================

# risk — уровень по умолчанию; policy.yaml может переопределить для каждого
# инструмента. chat — не инструмент, а маршрут в облачную LLM.
INTENTS = {
    'chat':           Intent('chat', 'общий диалог, сложные или неясные запросы',
                             RISK_LOW, fallback=True),
    'open_app':       Intent('open_app', 'открыть установленное приложение',
                             RISK_LOW),
    'open_file':      Intent('open_file', 'открыть файл в системе (xdg-open)',
                             RISK_MEDIUM),
    'open_url':       Intent('open_url', 'открыть URL в браузере (http/https)',
                             RISK_MEDIUM),
    'search_files':   Intent('search_files', 'найти файлы по имени (fd)',
                             RISK_LOW),
    'search_text':    Intent('search_text', 'найти текст в файлах (ripgrep)',
                             RISK_LOW),
    'volume_up':      Intent('volume_up', 'увеличить громкость (wpctl/pactl)',
                             RISK_LOW),
    'volume_down':    Intent('volume_down', 'уменьшить громкость (wpctl/pactl)',
                             RISK_LOW),
    'brightness_up':  Intent('brightness_up', 'увеличить яркость (brightnessctl)',
                             RISK_LOW),
    'brightness_down': Intent('brightness_down', 'уменьшить яркость (brightnessctl)',
                              RISK_LOW),
    'notify':         Intent('notify', 'показать уведомление (notify-send)',
                             RISK_LOW),
    'media_play_pause': Intent('media_play_pause', 'управление медиаплеером (playerctl)',
                               RISK_LOW),
    'lock_screen':    Intent('lock_screen', 'заблокировать экран', RISK_LOW),
    'screenshot':     Intent('screenshot', 'сделать скриншот', RISK_MEDIUM),
    'move_to_trash':  Intent('move_to_trash', 'переместить файл в корзину',
                             RISK_HIGH),
    # ---------------------------- время ------------------------------------
    'set_timer':      Intent('set_timer', 'установить таймер (systemd-run + уведомление)',
                             RISK_LOW),
    'set_alarm':      Intent('set_alarm', 'установить будильник на время (HH:MM)',
                             RISK_LOW),
    'set_reminder':   Intent('set_reminder', 'напоминание в заданное время (SQLite)',
                             RISK_LOW),
    'check_time':     Intent('check_time', 'который час (локальное время)', RISK_LOW),
    'check_date':     Intent('check_date', 'сегодняшняя дата и день недели', RISK_LOW),
    'list_reminders': Intent('list_reminders', 'список активных напоминаний',
                             RISK_LOW),
    'cancel_reminder': Intent('cancel_reminder', 'отменить напоминание (id или все)',
                              RISK_LOW),
    # ---------------------------- окна --------------------------------------
    'minimize_window': Intent('minimize_window', 'свернуть окно приложения (wmctrl)',
                              RISK_LOW),
    'maximize_window': Intent('maximize_window', 'развернуть окно приложения (wmctrl)',
                              RISK_LOW),
    'close_window':    Intent('close_window', 'закрыть окно приложения (wmctrl)',
                              RISK_MEDIUM),
    'list_windows':    Intent('list_windows', 'список открытых окон (wmctrl -l)',
                              RISK_LOW),
    'switch_window':   Intent('switch_window', 'переключиться на окно приложения (wmctrl)',
                              RISK_LOW),
    'switch_workspace': Intent('switch_workspace', 'переключить рабочий стол (wmctrl)',
                               RISK_LOW),
    # ---------------------------- текст -------------------------------------
    'count_words':     Intent('count_words', 'посчитать слова в тексте', RISK_LOW),
    'change_case':     Intent('change_case', 'сменить регистр текста (верхний/нижний)',
                              RISK_LOW),
    'translate_text':  Intent('translate_text', 'перевести текст (бесплатный API)',
                              RISK_LOW),
    # ---------------------------- система -----------------------------------
    'system_info':     Intent('system_info', 'информация о системе (OS, CPU, RAM)',
                              RISK_LOW),
    'check_disk':      Intent('check_disk', 'сколько места на диске', RISK_LOW),
    'check_battery':   Intent('check_battery', 'заряд батареи ноутбука', RISK_LOW),
    'check_network':   Intent('check_network', 'проверка сети и подключения',
                              RISK_LOW),
    'list_processes':  Intent('list_processes', 'список процессов (по памяти)',
                              RISK_LOW),
    'kill_process':    Intent('kill_process', 'завершить процесс по PID',
                              RISK_HIGH),
    # ---------------------------- калькулятор -------------------------------
    'calculate':       Intent('calculate', 'математические вычисления (безопасный парсер)',
                              RISK_LOW),
    'convert_currency': Intent('convert_currency', 'конвертация валют (кэш + API)',
                               RISK_LOW),
    'convert_units':   Intent('convert_units', 'конвертация единиц измерения',
                              RISK_LOW),
    # ---------------------------- буфер обмена ------------------------------
    'clipboard_copy':   Intent('clipboard_copy', 'скопировать текст в буфер обмена',
                               RISK_LOW),
    'clipboard_paste':  Intent('clipboard_paste', 'показать содержимое буфера обмена',
                               RISK_LOW),
    'clipboard_history': Intent('clipboard_history', 'история буфера обмена (своя)',
                                RISK_LOW),
    # ---------------------------- внешние сервисы ---------------------------
    'check_weather':   Intent('check_weather', 'погода в городе (wttr.in)', RISK_LOW),
    'check_news':      Intent('check_news', 'свежие новости из RSS-фидов', RISK_LOW),
    'send_email':      Intent('send_email', 'отправить письмо по SMTP', RISK_HIGH),
    'check_calendar':  Intent('check_calendar', 'ближайшие события из .ics', RISK_LOW),
}

# Порядок классов для обучения модели (стабильный, по алфавиту — чтобы
# порядок не зависел от порядка вставки в dict)
CLASS_NAMES = sorted(INTENTS)

# Псевдонимы-подсказки для описаний (для промпта облачной LLM)
RISK_LABELS_RU = {
    RISK_LOW: 'низкий (без подтверждения)',
    RISK_MEDIUM: 'средний (проверка путей, без подтверждения)',
    RISK_HIGH: 'высокий (требует подтверждения пользователя)',
}


def intent_by_name(name):
    return INTENTS.get(name)


def extract_slots(intent_name, text):
    intent = INTENTS.get(intent_name)
    return intent.extract_slots(text) if intent else {}