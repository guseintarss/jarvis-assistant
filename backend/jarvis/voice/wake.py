"""Распознавание слова-активатора в тексте (портировано из легаси-демона).

Маленькая Vosk-модель искажает слово «Ева» («ево», «йова», «вева», «еваа»),
поэтому кроме точного шаблона используется нечёткое совпадение. Слова, где
«ева/эва» лишь буквально присутствуют («дева», «лева», «евро»), ассистента
не активируют.
"""

import difflib
import re
import threading

from jarvis import config

# Событие прерывания: повторное слово «Ева» во время обработки/озвучки
# останавливает текущий запрос, и демон слушает новую команду.
interrupt_event = threading.Event()

# Фонетический шаблон слова-активатора: из «Ева» получается примерно
# «[йъь]?[еэ]в[аоуы]» — распознанное слово может начинаться с призвука
# «й/ъ/ь», первая гласная «е/э» взаимозаменяема, последняя — любая.
# «дева», «нева», «лева» шаблону не совпадают.


def _build_wake_pattern():
    for w in config.WAKE_WORDS:
        w = w.lower().strip()
        if not w:
            continue
        lead, core, tail = w[0], w[1:-1], w[-1]
        lead_cls = '[еэ]' if lead in 'еэ' else re.escape(lead)
        tail_cls = '[аоуы]' if tail in 'аоуы' else re.escape(tail)
        return re.compile(f'^[йъь]?{lead_cls}{re.escape(core)}{tail_cls}')
    return re.compile(r'^$')  # не должно случиться: WAKE_WORDS непуст


# Слова, где «ева/эва» — лишь окончание: на них ассистент просыпаться не должен.
_WAKE_FALSE_FRIENDS = frozenset((
    'дева', 'нева', 'лева', 'дива', 'лива', 'слива', 'нива', 'тива',
    'тиво', 'вева', 'еве', 'ёва', 'йова', 'жива', 'живе', 'рева',
    'евро', 'евра', 'евре', 'евва',
))

# Порог нечёткого совпадения слова с «Ева» (SequenceMatcher.ratio):
# 0.75 пропускает «йева», «еваа»-подобные огрехи модели, но отсекает
# «ава», «тва», «ява» и посторонние слова.
_WAKE_FUZZY_RATIO = 0.75

_WAKE_PATTERN = _build_wake_pattern()


def _wake_fuzzy_match(token):
    """Нечёткое совпадение слова с одним из вариантов слова-активатора.
    Слово должно НАЧИНАТЬСЯ как «Ева» (возможен призвук «й/ъ/ь») — иначе
    это чужое слово, в котором «ева» лишь буквально присутствует."""
    if len(token) < 3 or len(token) > 5:
        return False
    if token in _WAKE_FALSE_FRIENDS:
        return False
    if not re.match(r'^[йъь]?[еэ]', token):
        return False
    for w in config.WAKE_WORDS:
        w = w.lower().strip()
        if not w:
            continue
        if difflib.SequenceMatcher(None, token, w).ratio() >= _WAKE_FUZZY_RATIO:
            return True
    return False


def _wake_token_match(token):
    """Длина совпадения начала слова с словом-активатором: точный шаблон
    даёт длину совпадения в начале слова, нечёткий — всё слово целиком.
    Возвращает 0, если слово не похоже на активацию."""
    token = token.lower()
    m = _WAKE_PATTERN.search(token)
    if m:
        return m.end()
    return len(token) if _wake_fuzzy_match(token) else 0


def contains_wake_word(text):
    """True, если в распознанном тексте есть слово-активатор — точное
    («ева», «эва»), похожие варианты («ево», «йева») или нечёткие
    искажения модели («йова», «вева»). Проверяем отдельными токенами,
    чтобы «дева», «нева», «лева» не активировали ассистента случайно."""
    text = (text or '').lower()
    return any(_wake_token_match(t) > 0 for t in re.findall(r'[а-яё]+', text))


def strip_wake_word(text):
    """Убирает слово-активатор из начала фразы («ева, открой браузер»
    → «открой браузер»), чтобы модель не получала лишнего слова.
    Слова «дева», «лева» не трогаем."""
    m = re.match(r'^(?P<pre>[^а-яё]*)(?P<word>[а-яё]+)', text or '', re.IGNORECASE)
    if not m:
        return text
    match_len = _wake_token_match(m.group('word').lower())
    if not match_len:
        return text
    cut = m.start('word') + match_len
    return text[cut:].lstrip(' ,.…-:')