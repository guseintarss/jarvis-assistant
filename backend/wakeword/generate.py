# -*- coding: utf-8 -*-
"""ШАГ 2. Генерация датасета: TTS-позитив + confusable/речь/шум — негатив.

Идея: у нас нет тысяч записей голоса, поэтому:
- ПОЗИТИВ: синтезируем «Ева»/«Ева» разными голосами и темпами
  (espeak-ng всегда есть в системе; RHVoice/Piper — если стоят).
  Вариация темпа (скорость слов/мин) даёт разные длительности и артикуляцию.
- НЕГАТИВ:
  * confusable — слова, звучащие близко к «Ева» (Гарвис, Джава, Жарков,
    Дворник...) — hard negatives, без них модель ложно срабатывает;
  * речь — случайные команды/фразы (бытовые, как в реальном использовании);
  * шум — белый/розовый/бурый, тишина, babble (наложение речи) — фон комнаты.

Дальше (dataset.py) каждое слово кладётся в окно 1.0 с со случайным сдвигом
и «полом» из слабого шума, + аугментация на лету при обучении.

Использование:  python -m wakeword.generate --out data --pos 40 --neg 60
(ваши собственные записи «Ева» добавьте через python -m wakeword.record,
они будут перемешаны с TTS-примерами автоматически).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from .features import (SAMPLE_RATE, load_wav, noise_at_rms, resample_linear,
                       rms, save_wav, scale_to_rms, white_noise)

POS_RU = ["Ева", "Ева!", "Ева,", "Ева.", "ева"]
POS_EN = ["Eva", "Eva!", "Eva."]

# Confusable: рифмуются/звучат похоже — главный источник ложных срабатываний
NEG_CONFUSABLE = [
    "Дева", "Лева", "Нева", "Эва", "Сева", "Ява", "Егор", "Евгений",
    "Евгения", "Вера", "Жева", "Евану", "Эля", "Вея", "Веер", "Ель",
    "Еваль", "Левая", "Девять", "Евка", "Эвелина",
]

# Обычная бытовая речь — модель не должна срабатывать на команды
NEG_SPEECH = [
    "Привет", "Пока", "Открой браузер", "Который час", "Сколько времени",
    "Включи музыку", "Расскажи анекдот", "Сделай громче", "Сделай тише",
    "Стоп", "Пауза", "Дальше", "Назад", "Выключи свет", "Открой калькулятор",
    "Запусти терминал", "Какая погода", "Новости", "Купить молоко",
    "Завтра рано вставать", "Алло", "Слушаю", "Да", "Нет", "Продолжай",
]

# Скорости espeak-ng (слов/мин, норма ~175): вариация темпа произношения
ESPEAK_RATES = (130, 150, 170, 190, 210)
# Относительные темпа для RHVoice (length_scale) и piper (--length-scale)
RATE_SCALES = (0.85, 0.95, 1.05, 1.15)


# ---------------------------------------------------------------------------
# TTS-движки (каждый возвращает float32 16k моно)
# ---------------------------------------------------------------------------

def _tts_espeak(text: str, voice: str, rate: int) -> np.ndarray | None:
    """espeak-ng: -s скорость, -v голос, -w WAV (22050 Гц). Встроен в систему."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        subprocess.run(
            ["espeak-ng", "-v", voice, "-s", str(rate), "-w", path, text],
            check=True, capture_output=True, timeout=30,
        )
        x, sr = load_wav(path)
        return resample_linear(x, sr, SAMPLE_RATE)
    except Exception:
        return None
    finally:
        Path(path).unlink(missing_ok=True)


def _tts_rhvoice(text: str, rate_scale: float) -> np.ndarray | None:
    """RHVoice (голос Elena, если установлен): длина/темп через length_scale."""
    try:
        from rhvoice_wrapper import RHVoice
    except Exception:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        audio = RHVoice(text=text, voice="elena", length_scale=rate_scale)
        Path(path).write_bytes(audio)
        x, sr = load_wav(path)
        return resample_linear(x, sr, SAMPLE_RATE)
    except Exception:
        return None
    finally:
        Path(path).unlink(missing_ok=True)


def _tts_piper(text: str, rate_scale: float, model: Path | None) -> np.ndarray | None:
    """Piper (irina): --length-scale меняет длительность произношения."""
    if model is None or not model.exists():
        return None
    piper = Path(__import__("sys").prefix) / "bin" / "piper"
    if not piper.exists():
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        subprocess.run(
            [str(piper), "--model", str(model), "--length-scale",
             str(rate_scale), "--output_file", path, text],
            check=True, capture_output=True, timeout=30,
        )
        x, sr = load_wav(path)
        return resample_linear(x, sr, SAMPLE_RATE)
    except Exception:
        return None
    finally:
        Path(path).unlink(missing_ok=True)


def _tts_all(text: str, is_ru: bool, cfg) -> list[np.ndarray]:
    """Все доступные голоса/темпы -> список float32 16k."""
    out = []
    voice_ru, voice_en = cfg["espeak_voices"]
    if is_ru:
        for rate in ESPEAK_RATES:
            x = _tts_espeak(text, voice_ru, rate)
            if x is not None:
                out.append(x)
    else:
        for rate in ESPEAK_RATES:
            x = _tts_espeak(text, voice_en, rate)
            if x is not None:
                out.append(x)
    for scale in RATE_SCALES:
        x = _tts_rhvoice(text, scale)
        if x is not None:
            out.append(x)
        x = _tts_piper(text, scale, cfg.get("piper_model"))
        if x is not None:
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def _synthesize_word(text: str, is_ru: bool, cfg) -> list[np.ndarray]:
    """Слово + его вариации темпа. Отбрасываем слишком длинные (>0.95 с):
    окно 1.0 с должно вмещать слово с запасом на паузу."""
    out = []
    for x in _tts_all(text, is_ru, cfg):
        if x is None or len(x) == 0:
            continue
        # тишину по краям ТТS обрезаем (espeak добавляет паузы)
        a, b = 0, len(x)
        while a < b and abs(x[a]) < 1e-3:
            a += 1
        while b > a and abs(x[b - 1]) < 1e-3:
            b -= 1
        x = x[a:b]
        if 0.2 * SAMPLE_RATE < len(x) <= 0.95 * SAMPLE_RATE:
            out.append(scale_to_rms(x, 0.3))  # единая громкость
    return out


def _synthesize_noise(cfg) -> dict:
    """Шумовые «слова»: окно 1с каждого типа, плюс babble (наложение речи)."""
    rng = np.random.default_rng(cfg["seed"] + 7)
    kinds = {
        "noise_white": noise_at_rms("white", SAMPLE_RATE, 0.10, rng),
        "noise_pink": noise_at_rms("pink", SAMPLE_RATE, 0.12, rng),
        "noise_brown": noise_at_rms("brown", SAMPLE_RATE, 0.12, rng),
        # «тишина» = тихий белый шум (абсолютный ноль для float32 опасен:
        # log-mel без сигнала даёт -230, сеть к такому не привыкла)
        "noise_silence": scale_to_rms(white_noise(SAMPLE_RATE, rng), 0.002),
    }
    # babble: 6 чужих фраз поверх друг друга (шум кафе/офиса)
    phrases = np.array(NEG_SPEECH)
    rng.shuffle(phrases)
    babble = np.zeros(SAMPLE_RATE, dtype=np.float32)
    for i, ph in enumerate(phrases[:6]):
        x = _tts_espeak(str(ph), cfg["espeak_voices"][0], 170)
        if x is None:
            continue
        x = x[:SAMPLE_RATE]  # фраза не длиннее окна 1с
        start = rng.integers(0, max(1, SAMPLE_RATE - len(x)))
        babble[start:start + len(x)] += x * 0.5
    kinds["noise_babble"] = scale_to_rms(babble, 0.12)
    return kinds


def main() -> None:
    ap = argparse.ArgumentParser(description="Генерация датасета wake-word")
    ap.add_argument("--out", default="data", help="каталог для wav-файлов")
    ap.add_argument("--pos", type=int, default=40, help="позитивов на слово")
    ap.add_argument("--neg", type=int, default=60, help="негативов на категорию")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out_dir = Path(args.out)
    pos_dir, neg_dir = out_dir / "pos", out_dir / "neg"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    cfg = {
        "seed": args.seed,
        "espeak_voices": ("ru", "en-us"),
        "piper_model": next(Path.home().glob(
            ".local/share/jarvis-assistant/models/piper/*.onnx"), None),
    }

    meta = {"pos": [], "neg": [], "confusable": []}
    counters = {"pos": 0, "neg": 0}  # глобальные индексы (не сбрасывать!)

    def save(words: list[np.ndarray], prefix: str, d: Path,
             meta_list: list, key: str) -> None:
        for x in words:
            p = d / f"{prefix}_{counters[key]:04d}.wav"
            save_wav(str(p), x)
            meta_list.append(p.name)
            counters[key] += 1

    print("==> Позитив: «Ева» (ru) и «Eva» (en), все голоса и темпы")
    for text in POS_RU + POS_EN:
        is_ru = text[0].isalpha() and any(
            c.isalpha() and ord(c) > 127 for c in text)
        w = _synthesize_word(text, is_ru, cfg)
        save(w, "pos", pos_dir, meta["pos"], "pos")
        print(f"    {text!r}: {len(w)} примеров")

    print("==> Негатив: confusable слова (hard negatives)")
    for text in NEG_CONFUSABLE:
        w = _synthesize_word(text, True, cfg)
        save(w, "neg_conf", neg_dir, meta["neg"], "neg")
    print(f"    {len(NEG_CONFUSABLE)} слов")

    print("==> Негатив: бытовая речь")
    for text in NEG_SPEECH:
        w = _synthesize_word(text, True, cfg)
        save(w, "neg_speech", neg_dir, meta["neg"], "neg")
    print(f"    {len(NEG_SPEECH)} фраз")

    print("==> Негатив: шум (белый/розовый/бурый/тишина/babble)")
    for name, x in _synthesize_noise(cfg).items():
        save([x], name, neg_dir, meta["neg"], "neg")
        print(f"    {name}: 1 окно 1с")

    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"==> Готово: {len(meta['pos'])} позитивов, "
          f"{len(meta['neg'])} негативов в {out_dir}/")


if __name__ == "__main__":
    main()