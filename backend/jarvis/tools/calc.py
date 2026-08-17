"""Калькулятор и конвертеры: безопасный арифметический парсер, валюты,
единицы измерения.

    calculate        — ast.parse + ручная обёртка узлов (БЕЗ eval/exec):
                       + - * / % ** скобки, числа, pi/e, sqrt/abs/round;
    convert_currency — курсы из офлайн-кэша (24 ч), обновление с бесплатного
                       open.er-api.com; при отсутствии сети — последний кэш;
    convert_units    — таблица групп единиц (длина/масса/объём/время/данные/
                       температура).

Выражение валидируется по белому списку узлов AST: никакие имена,
атрибуты, вызовы произвольных функций и подстроки невозможны.
"""

import ast
import json
import math
import os
import re
import time

import requests

from jarvis import config

# ============================== БЕЗОПАСНЫЙ ПАРСЕР ===========================

# Разрешённые функции (вызываются по имени из этого словаря, не иначе)
_SAFE_FUNCS = {
    'sqrt': math.sqrt,
    'abs': abs,
    'round': round,
    'min': min,
    'max': max,
    'pow': pow,
}

# Разрешённые константы
_SAFE_CONSTS = {'pi': math.pi, 'e': math.e}

# Разрешённые узлы AST
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)


def _eval_node(node):
    """Рекурсивно вычисляет узел AST; TypeError/ValueError -> исключение."""
    if not isinstance(node, _ALLOWED_NODES):
        raise ValueError('выражение содержит запрещённые операции')
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError('допустимы только числа')
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError
            return left / right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
        raise ValueError('неизвестная операция')
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        raise ValueError('неизвестная унарная операция')
    raise ValueError('неподдерживаемый узел')


def calculate(expression=''):
    """Безопасно вычисляет арифметическое выражение из реплики.

    Принимает русские слова-операции: «плюс», «минус», «умножить на»,
    «разделить на», «процентов от» (10% от 200 = 20).
    """
    expr = (expression or '').strip()
    if not expr:
        return False, 'Скажите выражение (например, «сколько будет 25 умножить на 37»).'

    words = {
        'плюс': '+', 'и': '+', 'минус': '-', 'умножить на': '*',
        'умноженное на': '*', 'разделить на': '/', 'поделить на': '/',
        'деленное на': '/', 'процентов от': '/100*', 'процента от': '/100*',
        'процент от': '/100*', 'сколько будет': '', 'сколько': '',
        'будет': '', 'целых': '.', 'запятая': '.',
    }
    expr = re.sub(r'\s+', ' ', expr.lower())
    # многоключевые замены («умножить на») — ДО одиночных («на» не трогаем)
    for ru, sign in sorted(words.items(), key=lambda kv: -len(kv[0])):
        expr = re.sub(rf'\b{ru}\b', sign, expr)
    expr = re.sub(r'(?<=\d)\s*,\s*(?=\d)', '.', expr)
    expr = expr.replace('^', '**').replace('х', '*').replace('×', '*')

    # «10 процентов от 200» -> «10 /100 * 200»
    expr = re.sub(r'(\d+(?:\.\d+)?)\s*/\s*100\s*\*\s*(\d+(?:\.\d+)?)',
                  r'(\1 / 100) * \2', expr)
    expr = expr.strip()  # после удаления «сколько будет» может остаться пробел

    try:
        tree = ast.parse(expr, mode='eval')
        result = _eval_node(tree.body)
    except (SyntaxError, ValueError, ZeroDivisionError) as exc:
        return False, f'Не смог посчитать: {exc}'
    except TypeError:
        return False, 'Не смог посчитать: выражение слишком сложное.'

    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        return False, 'Результат не определён (выходит за допустимые пределы).'
    text = f'{result:g}' if isinstance(result, float) and abs(result) < 1e15 \
        else f'{result:,}'.replace(',', ' ')
    return True, f'{expression.strip()} = {text}'


# ============================== ВАЛЮТЫ =======================================

# Русские названия валют -> коды ISO 4217
_CURRENCIES = {
    'доллар': 'USD', 'евро': 'EUR', 'рубл': 'RUB', 'фунт': 'GBP',
    'иен': 'JPY', 'юан': 'CNY', 'франк': 'CHF', 'тенг': 'KZT',
    'гривн': 'UAH', 'лир': 'TRY', 'крон': 'SEK',
}


def _currency_code(name):
    if not name:
        return None
    n = name.lower().strip()
    if n.startswith('в '):
        n = n[2:].strip()  # слот 'to' приходит как «в евро»
    for stem, code in _CURRENCIES.items():
        if n.startswith(stem):
            return code
    return None


def _load_rates():
    """Курсы из кэша: dict код->курс к USD + свежесть. Без сети — кэш."""
    try:
        with open(config.CURRENCY_CACHE_PATH, encoding='utf-8') as f:
            data = json.load(f)
        age = os.path.getmtime(config.CURRENCY_CACHE_PATH)
        rates = data.get('rates') or {}
        if time.time() - age < config.CURRENCY_CACHE_MAX_AGE_SEC:
            return rates, True
        return rates, False  # кэш есть, но устарел
    except (OSError, ValueError):
        return {}, False


def _update_rates():
    """Обновляет кэш курсов с open.er-api.com; False при ошибке сети."""
    try:
        r = requests.get(config.CURRENCY_API_URL,
                         timeout=config.CURRENCY_API_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        rates = data.get('rates') or {}
        if not rates:
            return False
        os.makedirs(os.path.dirname(config.CURRENCY_CACHE_PATH),
                    exist_ok=True)
        with open(config.CURRENCY_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'rates': rates, 'base': 'USD'}, f, ensure_ascii=False)
        return True
    except (requests.RequestException, ValueError):
        return False


def convert_currency(amount=None, from_=None, to_=None):
    """Конвертация валют: amount единиц from в to (кэш + обновление)."""
    try:
        value = float((amount or '1').replace(',', '.'))
    except ValueError:
        return False, 'Не понял сумму для конвертации.'
    src = _currency_code(from_ or '')
    dst = _currency_code(to_ or '')
    if not src or not dst:
        return False, ('Не понял валюты. Скажите, например, «переведи 100 '
                       'долларов в евро».')
    rates, fresh = _load_rates()
    if src not in rates and not _update_rates():
        return False, ('Нет кэша курсов и нет сети для обновления. '
                       'Попробуйте позже.')
    if src not in rates or dst not in rates:
        rates, fresh = {}, False
        if not _update_rates():
            return False, 'Не удалось получить курсы валют (нет сети).'
        rates = _load_rates()[0]
    if src not in rates or dst not in rates:
        return False, f'Нет курса для {src}/{dst}.'
    result = value / rates[src] * rates[dst]
    return True, (f'{value:g} {src} = {result:,.2f} {dst}'
                  + ('' if fresh else ' (курс из кэша)'))


# ============================== ЕДИНИЦЫ =====================================

# (группа, базисный множитель); имя -> (группа, множитель)
_UNITS = {
    'километр': ('length', 1000.0), 'километров': ('length', 1000.0),
    'мет': ('length', 1.0),
    'сантиметр': ('length', 0.01),
    'миллиметр': ('length', 0.001),
    'килограмм': ('mass', 1.0),
    'грамм': ('mass', 0.001),
    'тонн': ('mass', 1000.0),
    'литр': ('volume', 1.0),
    'миллилитр': ('volume', 0.001),
    'час': ('time', 3600.0),
    'минут': ('time', 60.0),
    'секунд': ('time', 1.0),
    'байт': ('data', 1.0),
    'килобайт': ('data', 1024.0),
    'мегабайт': ('data', 1024.0 ** 2),
    'гигабайт': ('data', 1024.0 ** 3),
}

_TEMPERATURES = {'цельсия': 'C', 'фаренгейта': 'F', 'цельсиях': 'C',
                 'фаренгейтах': 'F'}


def _unit_group(name):
    """Имя единицы -> (группа, фактор-код/множитель)."""
    n = (name or '').lower().strip()
    if n.startswith('в '):
        n = n[2:].strip()  # слот 'to' приходит как «в евро»
    if 'цельсия' in n or 'цельсиях' in n:
        return 'temperature', 'C'
    if 'фаренгейта' in n or 'фаренгейтах' in n:
        return 'temperature', 'F'
    for stem, (group, factor) in _UNITS.items():
        if n.startswith(stem):
            return group, factor
    return None, None


def convert_units(amount=None, from_=None, to_=None):
    """Конвертация единиц внутри группы (метрические + температура)."""
    try:
        value = float((amount or '1').replace(',', '.'))
    except ValueError:
        return False, 'Не понял количество.'
    src_group, src = _unit_group(from_ or '')
    dst_group, dst = _unit_group(to_ or '')
    if src is None or dst is None:
        return False, ('Не понял единицы. Скажите, например, «переведи '
                       '5 километров в метры».')
    if src_group != dst_group:
        return False, (f'Нельзя перевести {src_group} в {dst_group}: '
                       'разные группы единиц.')
    if src_group == 'temperature':
        if src == dst:
            result = value
        elif src == 'C':
            result = value * 9 / 5 + 32
        else:
            result = (value - 32) * 5 / 9
        return True, f'{value:g}°{src} = {result:g}°{dst}'
    result = value * src / dst
    return True, f'{value:g} {from_} = {result:g} {to_}'


TOOLS = {
    'calculate': calculate,
    'convert_currency': convert_currency,
    'convert_units': convert_units,
}