"""Текстовые инструменты: счёт слов, регистр, перевод.

    count_words    — len(text.split()) — мгновенно, без нейросетей;
    change_case    — верхний/нижний регистр (str.upper/lower/title);
    translate_text — бесплатный API MyMemory (без ключа и регистрации);
                     если сети нет — вежливая ошибка с подсказкой.

Секретов в запросах к API нет; длина текста ограничивается сверху.
"""

import re
import time

import requests

from jarvis import config

# ============================== СЧЁТ СЛОВ ===================================


def count_words(text=''):
    """Считает слова в переданном тексте."""
    phrase = (text or '').strip()
    if not phrase:
        return False, 'Скажите, какой текст посчитать (например, «посчитай слова в этом предложении»).'
    words = [w for w in re.split(r'[\s,.;:!?«»"()]+', phrase) if w]
    return True, f'В тексте {len(words)} слов: «{phrase[:80]}»'


# ============================== РЕГИСТР =====================================


def change_case(case=None, text=''):
    """Переводит текст в верхний/нижний/заглавный регистр."""
    phrase = (text or '').strip()
    if not phrase:
        return False, 'Скажите, какой текст изменить (например, «сделай текст заглавными буквами»).'
    case = (case or '').lower()
    if any(k in case for k in ('верхн', 'заглавн', 'прописн')):
        return True, phrase.upper()
    if any(k in case for k in ('нижн', 'строчн')):
        return True, phrase.lower()
    return True, phrase.title()


# ============================== ПЕРЕВОД =====================================

_LANG = {
    'английск': 'en',
    'русск': 'ru',
    'немецк': 'de',
    'испанск': 'es',
    'французск': 'fr',
    'итальянск': 'it',
}
_LANG_REV = {v: k for k, v in _LANG.items()}


def translate_text(lang=None, text=''):
    """Перевод через бесплатный MyMemory API: ru->lang / lang->ru."""
    phrase = (text or '').strip()
    if not phrase:
        return False, 'Скажите, что перевести (например, «переведи привет на английский»).'
    lang_key = None
    for stem, code in _LANG.items():
        if (lang or '') and stem in lang:
            lang_key = code
            break
    if lang_key is None:
        lang_key = 'en'  # по умолчанию — на английский
    # язык-источник: если фраза латиницей, считаем исходным английским
    src, dst = ('ru', lang_key) if not re.search(r'[a-z]', phrase) else ('en', lang_key)
    if dst == src:
        dst = 'ru' if src == 'en' else 'en'
    url = config.TRANSLATE_API_URL.format(
        query=requests.utils.quote(phrase[:2000]), src=src, dst=dst)
    try:
        r = requests.get(url, timeout=config.TRANSLATE_API_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        translated = (data.get('responseData') or {}).get('translatedText') or ''
        if not translated or translated == phrase:
            return False, 'Переводчик не смог перевести эту фразу (попробуйте короче).'
        lang_name = _LANG_REV.get(dst, dst)
        return True, f'Перевод на {lang_name}: {translated}'
    except (requests.RequestException, OSError, ValueError):
        return False, ('Перевод недоступен: нет сети или сервис не отвечает. '
                       'Повторите позже.')


TOOLS = {
    'count_words': count_words,
    'change_case': change_case,
    'translate_text': translate_text,
}