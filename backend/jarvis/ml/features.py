"""Признаки текста для классификатора.

Идея: для русского языка надёжнее всего работают символьные n-граммы —
они устойчивы к опечаткам, словоформам и незнакомым словам. Признаки
хэшируются (zlib.crc32) в вектор фиксированной размерности, поэтому
словарь не нужен и нет проблемы OOV-слов.

Важно: хэш детерминированный (crc32), а не встроенный hash() —
иначе из-за соли PYTHONHASHSEED модель обучалась бы и предсказывала
в разных пространствах признаков.
"""

import re
import zlib

import numpy as np

# Символьные n-граммы и длина слов
NGRAM_SIZES = (2, 3, 4)


def _hgram(gram, dim):
    """Детерминированный хэш строки в индекс [0, dim)."""
    return zlib.crc32(gram.encode('utf-8')) % dim


def vectorize(text, dim=4096):
    """Одна строка -> вектор признаков float32 (нормирован по L2)."""
    text = text.lower()
    vec = np.zeros(dim, dtype=np.float32)

    # Символьные n-граммы (с границами '^' и '$', чтобы 'открой' != 'крой')
    norm = '^' + re.sub(r'[^а-яёa-z0-9]+', '', text) + '$'
    for n in NGRAM_SIZES:
        for i in range(len(norm) - n + 1):
            vec[_hgram(norm[i:i + n], dim)] += 1.0

    # Целые слова (для устойчивости длинных осмысленных токенов)
    for word in re.findall(r'[а-яёa-z0-9]{3,}', text):
        vec[_hgram('w:' + word, dim)] += 1.0

    norm_l2 = float(np.linalg.norm(vec))
    if norm_l2 > 0:
        vec /= norm_l2
    return vec


def vectorize_many(texts, dim=4096):
    """Список строк -> матрица признаков (N, dim)."""
    rows = [vectorize(t, dim) for t in texts]
    return np.vstack(rows).astype(np.float32) if rows else \
        np.zeros((0, dim), dtype=np.float32)