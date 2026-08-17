# -*- coding: utf-8 -*-
"""Запись собственных образцов «Ева» (и фонового шума комнаты).

Почему это важно: синтетика (TTS) покрывает артикуляцию, но модель лучше
всего срабатывает на ОДНОМ голосе — вашем. Запишите 10-30 произношений
«Ева» + 30 секунд тишины комнаты (станет «полом» для позитивов и
негативом). Записи лягут в data/recorded и будут использованы наравне
с TTS при сборке окон (dataset.py).

Использование:
    python -m wakeword.record --out data --count 15     # слово, Enter между
    python -m wakeword.record --out data --noise 30     # шум комнаты 30с
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from .features import SAMPLE_RATE, rms, save_wav

BUF_SECONDS = 1.5  # записываем с запасом, тишину по краям обрежем


def _record_block(seconds: float) -> np.ndarray:
    """Синхронная запись с микрофона по умолчанию (float32 моно 16k)."""
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=1, dtype="float32", blocking=True)
    return audio[:, 0]


def _trim_silence(x: np.ndarray, thr: float = 0.005) -> np.ndarray:
    a, b = 0, len(x)
    while a < b and abs(x[a]) < thr:
        a += 1
    while b > a and abs(x[b - 1]) < thr:
        b -= 1
    return x[a:b]


def _has_speech(x: np.ndarray, min_rms: float = 0.02) -> bool:
    """Речь ли это: максимум RMS по 100 мс-окнам. Тихие промахи
    (слово вне окна записи) отклоняются — иначе тишина попадёт в
    ПОЗИТИВЫ датасета и научит модель считать фон «Евой»."""
    win = int(0.1 * SAMPLE_RATE)
    peak = 0.0
    for i in range(0, len(x) - win, win):
        peak = max(peak, rms(x[i:i + win]))
    return peak >= min_rms


def record_word(out_dir: Path, count: int) -> None:
    pos_dir = out_dir / "recorded" / "pos"
    pos_dir.mkdir(parents=True, exist_ok=True)
    print(f"Запись {count} произношений «Ева». Enter = запись, "
          f"q + Enter = выход")
    n = 0
    while n < count:
        if input(f"[{n + 1}/{count}] Enter — говорить, q — выход: ").strip() == "q":
            break
        x = _record_block(BUF_SECONDS)
        x = _trim_silence(x)
        if len(x) < 0.15 * SAMPLE_RATE:
            print("    слишком тихо/коротко, повторите")
            continue
        if not _has_speech(x):
            print("    речи не обнаружено (говорите в течение 1.5 с "
                  "после Enter), не сохранено")
            continue
        p = pos_dir / f"mine_{n:03d}.wav"
        save_wav(str(p), x)
        print(f"    сохранено {p} ({len(x) / SAMPLE_RATE:.2f} с)")
        n += 1


# Слова-«двойники» и речь для записи своим голосом (негативы)
NEG_WORDS_TO_RECORD = [
    # confusable: звучат почти как «Ева» — главный источник ложных срабатываний
    "Дева", "Лева", "Эва", "Сева", "Нева", "Ява", "Вера", "Егор", "Евгений",
    # обычная речь, с которой вы будете обращаться к ассистенту
    "Привет", "Открой браузер", "Который час", "Сделай громче",
    "Включи музыку", "Стоп", "Продолжай",
]


def record_neg(out_dir: Path) -> None:
    """Запись негативов своим голосом: подсказка -> Enter -> слово.
    Каждое слово по 3 раза (варьируйте интонацию/громкость)."""
    neg_dir = out_dir / "recorded" / "neg"
    neg_dir.mkdir(parents=True, exist_ok=True)
    print(f"Запись {len(NEG_WORDS_TO_RECORD)} слов/фраз x3 (негативы).")
    print("Вам будет показано слово — скажите ЕГО (не «Ева»). q + Enter — выход")
    n = 0
    for word in NEG_WORDS_TO_RECORD:
        for rep in range(3):
            if input(f"[{word!r} #{rep + 1}/3] Enter — говорить, q — выход: "
                     ).strip() == "q":
                return
            x = _record_block(BUF_SECONDS)
            x = _trim_silence(x)
            if len(x) < 0.15 * SAMPLE_RATE or not _has_speech(x):
                print("    не разобрал — повторите")
                continue
            p = neg_dir / f"mine_{n:03d}.wav"
            save_wav(str(p), x)
            print(f"    сохранено {p}")
            n += 1
    print(f"Готово: {n} негативов в {neg_dir}")


def record_noise(out_dir: Path, seconds: int) -> None:
    noise_dir = out_dir / "recorded" / "noise"
    noise_dir.mkdir(parents=True, exist_ok=True)
    print(f"Запись {seconds} с фонового шума комнаты (не говорите)...")
    x = _record_block(seconds)
    p = noise_dir / "room.wav"
    save_wav(str(p), x)
    print(f"    сохранено {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Запись своих образцов")
    ap.add_argument("--out", default="data")
    ap.add_argument("--count", type=int, default=15)
    ap.add_argument("--neg", action="store_true",
                    help="записать негативы (слова-двойники и речь)")
    ap.add_argument("--noise", type=int, default=0,
                    help="вместо слова записать N секунд шума комнаты")
    args = ap.parse_args()
    if args.noise > 0:
        record_noise(Path(args.out), args.noise)
    elif args.neg:
        record_neg(Path(args.out))
    else:
        record_word(Path(args.out), args.count)


if __name__ == "__main__":
    main()