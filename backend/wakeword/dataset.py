# -*- coding: utf-8 -*-
"""Окна 1.0 с + аугментация на лету (ШАГ 2, часть 2).

Из wav-«слов» собираются обучающие окна (X: (n, 16000) float32, y: 0/1):

ПОЗИТИВ: слово кладётся в СЛУЧАЙНУЮ позицию окна (сдвиг 0.1-0.55 с), а не
в начало — сеть учится «слово есть в окне», а не «слово начинается с 0-го
отсчёта». Остаток окна — слабый шум-пол (комнатный: розовый/запись комнаты,
RMS ~ -30 дБ от слова). Это имитирует реальную сцену «тихая комната +
говорящий».

НЕГАТИВ: окно целиком — шум, обрывок речи, babble; confusable-слова
кладём так же, как позитив (трудный случай: похожее слово в случайной позе).

Аугментация (применяется в __getitem__ при каждом обращении — модель
каждую эпоху видит «новый» сигнал, это и есть анsemble-эффект):
  - AddNoise: SNR 6-22 дБ (белый/розовый/бурый) — устойчивость к шуму;
  - SpeedJitter: 0.9-1.1x (линейный ресемплинг) — темп речи;
  - VolumeJitter: 0.6-1.4x — расстояние до микрофона;
  - Echo: комб-фильтр y[n] += 0.25*y[n-4000] (250 мс, отражение от стены);
  - Clipping: 5% — перегрузка микрофона (крики).
Все вероятности/разбросы — параметры класса.

Разбиение train/val: стратифицированное 80/20 (одинаковая доля классов).
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np

try:
    import torch
except ImportError:
    torch = None  # сборка окон работает без torch; обучение — нет

from .features import (SAMPLE_RATE, WIN_SAMPLES, load_wav, noise_at_rms,
                       resample_linear, rms, scale_to_rms)

# Сдвиг слова внутри окна (доля окна): [0.1, 0.6] — середина+запас,
# чтобы слово не упиралось в край окна (у «Ева» хвост тихий — край окна
# подрезал бы «а», а модель научилась бы узнавать обрубок).
POS_SHIFT_RANGE = (0.10, 0.60)
# Уровень шума-пола под словом (дБ от RMS слова), диапазон джиттера
FLOOR_DB_RANGE = (-38.0, -24.0)


# ---------------------------------------------------------------------------
# Сборка окон из списка wav
# ---------------------------------------------------------------------------

def _load_words(files: list[Path], rng: np.random.Generator
                ) -> list[np.ndarray]:
    out = []
    for p in files:
        try:
            x, sr = load_wav(str(p))
            out.append(resample_linear(x, sr, SAMPLE_RATE))
        except Exception:
            continue
    return out


def _trim_silence(x: np.ndarray, margin: int = 800) -> np.ndarray:
    """Обрезка тишины по краям. Реальные записи длиной 1.5 с содержат
    слово 0.3-0.5 с: без обрезки слово не помещается в окно 1.0 с, и
    позитив превращается в «тихий пол» (модель учит пол, а не слово)."""
    w = 320  # 20 мс кадр
    n_frames = len(x) // w
    if n_frames < 2:
        return x
    frames = x[:n_frames * w].reshape(-1, w)
    r = np.sqrt((frames ** 2).mean(axis=1))
    th = max(float(r.max()) * 0.02, 0.002)
    idx = np.flatnonzero(r > th)
    if len(idx) == 0:
        return x
    lo = max(0, int(idx[0]) * w - margin)
    hi = min(len(x), (int(idx[-1]) + 1) * w + margin)
    out = x[lo:hi]
    if len(out) >= WIN_SAMPLES:
        # Фон в записи не даёт отрезать «тишину» (микрофон с AGC) —
        # вырезаем 0.6 с вокруг пика энергии (слово — самая громкая часть).
        p = int(np.argmax(r))
        lo = max(0, p * w - 2400)   # 0.15 с до пика
        hi = min(len(x), p * w + 7200)  # 0.45 с после пика
        out = x[lo:hi]
    return out


def _place_word(word: np.ndarray, floor: np.ndarray,
                rng: np.random.Generator) -> np.ndarray:
    """Слово в случайной позиции поверх шума-пола."""
    win = floor.copy()
    word = _trim_silence(word)
    if len(word) >= WIN_SAMPLES:
        word = word[:WIN_SAMPLES]  # крайний случай — обрезаем
    max_start = WIN_SAMPLES - len(word) - 1
    start = int((rng.uniform(*POS_SHIFT_RANGE)) * WIN_SAMPLES)
    start = min(start, max_start)
    win[start:start + len(word)] += word
    return win


def _make_floor(rng: np.random.Generator, room_noise: Path | None,
                target_rms: float) -> np.ndarray:
    """Шум-пол окна: запись комнаты или розовый шум, уровень target_rms."""
    if room_noise is not None and room_noise.exists():
        try:
            floor_base, sr = load_wav(str(room_noise))
            floor_base = resample_linear(floor_base, sr, SAMPLE_RATE)
            if len(floor_base) < WIN_SAMPLES:
                floor_base = np.tile(
                    floor_base, int(np.ceil(WIN_SAMPLES / len(floor_base)))
                )[:WIN_SAMPLES]
            return scale_to_rms(floor_base, target_rms)
        except Exception:
            pass
    return noise_at_rms("pink", WIN_SAMPLES, target_rms, rng)


def build_windows(pos_wavs: list[Path], neg_wavs: list[Path],
                  room_noise: Path | None = None,
                  windows_per_pos: int = 8, windows_per_neg: int = 8,
                  seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """-> (X (n,16000) float32, y (n,) 0/1). По каждому wav — несколько окон."""
    rng = np.random.default_rng(seed)
    pos = _load_words(pos_wavs, rng)
    neg = _load_words(neg_wavs, rng)

    X, y = [], []
    # Позитив: слово на полу. Уровень пола джиттерится (-38..-24 дБ от
    # слова) — модель не должна «считать фон по громкости».
    # Реальные записи (mine_*) важнее синтетики: им 4x окон.
    for path in pos_wavs:
        mult = 4 if "mine" in path.name else 1
        w = pos[list(pos_wavs).index(path)]
        for _ in range(windows_per_pos * mult):
            db = rng.uniform(*FLOOR_DB_RANGE)
            floor = _make_floor(rng, room_noise, rms(w) * 10 ** (db / 20.0))
            X.append(_place_word(w, floor, rng))
            y.append(1)

    # КРИТИЧНЫЙ класс: «пол без слова» (тихая комната). Без него модель
    # учит «тихий фон = позитив» (в реальности фон есть ВСЕГДА).
    n_floor_neg = max(1, len(pos) * windows_per_pos // 2)
    for _ in range(n_floor_neg):
        db = rng.uniform(-38.0, -24.0)
        floor = _make_floor(rng, room_noise, 0.3 * 10 ** (db / 20.0))
        X.append(floor)
        y.append(0)

    # «Шум без слова» на уровнях аугментированных позитивов (SNR 10-25 дБ
    # от речи 0.3 => 0.017..0.095). Без этого модель учит «громкий шум =
    # Ева»: позитивы со шумом перебивают 5 шумовых файлов-негативов.
    n_noise_neg = max(1, len(pos) * windows_per_pos // 2)
    for _ in range(n_noise_neg):
        kind = ("white", "pink", "brown")[int(rng.integers(3))]
        db = rng.uniform(-25.0, -15.0)
        win = noise_at_rms(kind, WIN_SAMPLES, 0.3 * 10 ** (db / 20.0), rng)
        X.append(win)
        y.append(0)

    # Негатив: случайное слово/шум заполняет окно целиком (loop/обрезание)
    # confusable («Дева», «Лева»...) и РЕАЛЬНЫЕ записи — hard negatives,
    # им нужно больше окон, иначе крошечная сеть сольёт их с «Ева»
    neg_flags = [("conf" in p.name, "mine" in p.name,
                  "noise" in p.parent.name)
                 for p in neg_wavs]  # до загрузки wav
    for (is_conf, is_mine, is_noise), w in zip(neg_flags, neg):
        mult = 3 if is_conf else (2 if is_mine else 1)
        for _ in range(windows_per_neg * mult):
            if is_noise:
                # Шум без слова: окно целиком (loop/обрезание)
                if len(w) < WIN_SAMPLES:
                    reps = int(np.ceil(WIN_SAMPLES / len(w)))
                    win = np.tile(w, reps)[:WIN_SAMPLES]
                else:
                    start = rng.integers(0, len(w) - WIN_SAMPLES + 1)
                    win = w[start:start + WIN_SAMPLES]
            else:
                # Трудный негатив: слово кладётся ТАК ЖЕ, как позитив —
                # случайная позиция на полу. Иначе модель учит «одно слово =
                # Ева, повтор/сплошная речь = не-Ева» и в живом потоке
                # палит любую речь.
                db = rng.uniform(*FLOOR_DB_RANGE)
                floor = _make_floor(rng, room_noise, rms(w) * 10 ** (db / 20.0))
                win = _place_word(w, floor, rng)
            X.append(win)
            y.append(0)

    X = np.stack(X).astype(np.float32)
    y = np.asarray(y, dtype=np.float32)
    # стратифицированное перемешивание
    perm = rng.permutation(len(y))
    return X[perm], y[perm]


# ---------------------------------------------------------------------------
# Аугментация (аудио-домен, на лету)
# ---------------------------------------------------------------------------

class Augmenter:
    def __init__(self, noise_kinds=("white", "pink", "brown"),
                 snr_db=(10.0, 25.0), speed=(0.90, 1.10),
                 volume=(0.6, 1.4), echo_prob=0.3, clip_prob=0.05):
        # SNR >= 10 дБ: шум не должен заглушать слово (на SNR 6 дБ модель
        # перестаёт различать «слово+шум» и «просто шум» и учит шум позитивом)
        self.noise_kinds = noise_kinds
        self.snr_db = snr_db
        self.speed = speed
        self.volume = volume
        self.echo_prob = echo_prob
        self.clip_prob = clip_prob

    def __call__(self, x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        # 1. Скорость: передискретизация меняет длительность слова.
        #    SNR: шум с энергией E_signal / 10^(SNR/10)
        speed = rng.uniform(*self.speed)
        if abs(speed - 1.0) > 0.01:
            x = resample_linear(x, SAMPLE_RATE, int(SAMPLE_RATE * speed))
            # фиксируем длину окна: длинное обрезаем, короткое дополняем
            if len(x) > WIN_SAMPLES:
                x = x[:WIN_SAMPLES]
            elif len(x) < WIN_SAMPLES:
                x = np.concatenate(
                    [x, np.zeros(WIN_SAMPLES - len(x), dtype=np.float32)])
        # 2. Громкость
        x = x * rng.uniform(*self.volume)
        # 3. Аддитивный шум
        kind = self.noise_kinds[rng.integers(len(self.noise_kinds))]
        sig_rms = rms(x) + 1e-9
        snr = rng.uniform(*self.snr_db)
        noise_rms = sig_rms * 10 ** (-snr / 20.0)
        noise = noise_at_rms(kind, len(x), noise_rms, rng)
        x = x + noise
        # 4. Эхо (комб-фильтр: отражение через 250 мс)
        if rng.random() < self.echo_prob and len(x) > 4000:
            x = x + 0.25 * np.concatenate(
                [np.zeros(4000, dtype=np.float32), x[:-4000]])
        # 5. Клиппинг (перегрузка)
        if rng.random() < self.clip_prob:
            x = np.clip(x, -0.9, 0.9)
        return x


# ---------------------------------------------------------------------------
# PyTorch Dataset: аудио -> признаки (1, 40, 97) -> тензор
# ---------------------------------------------------------------------------

if torch is not None:

    class WakeDataset(torch.utils.data.Dataset):
        def __init__(self, X: np.ndarray, y: np.ndarray, extractor,
                     augment: bool = True, seed: int = 0):
            self.X, self.y = X, y
            self.extractor = extractor
            self.augmenter = Augmenter() if augment else None
            self._rng = np.random.default_rng(seed)
            self._rng_aug = np.random.default_rng(seed + 1)

        def __len__(self) -> int:
            return len(self.y)

        def __getitem__(self, i: int):
            x = self.X[i]
            if self.augmenter is not None:
                x = self.augmenter(x, self._rng_aug)
            f = self.extractor.transform(x)          # (40, 97) нормированные
            t = torch.from_numpy(f).unsqueeze(0)     # (1, 40, 97)
            return t, torch.tensor(self.y[i], dtype=torch.float32)
else:

    class WakeDataset:  # заглушка без torch (для инференса/тестов)
        def __init__(self, *args, **kwargs):
            raise ImportError('torch не установлен: обучение wakeword '
                              'недоступно (инференс через ONNX работает)')


def split_stratified(X: np.ndarray, y: np.ndarray, val_frac: float = 0.2,
                     seed: int = 0) -> tuple:
    """Стратифицированное 80/20 (одинаковые доли классов в train/val)."""
    rng = np.random.default_rng(seed)
    idx_pos = np.flatnonzero(y == 1)
    idx_neg = np.flatnonzero(y == 0)
    out = []
    for idx in (idx_pos, idx_neg):
        n = len(idx)
        n_val = int(round(n * val_frac))
        perm = rng.permutation(n)
        out.append((idx[perm[n_val:]], idx[perm[:n_val]]))
    tr = np.concatenate([out[0][0], out[1][0]])
    va = np.concatenate([out[0][1], out[1][1]])
    return X[tr], y[tr], X[va], y[va]