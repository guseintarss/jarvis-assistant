# -*- coding: utf-8 -*-
"""ШАГ 1. Признаки: log-mel спектрограмма / MFCC на чистом numpy.

Математика выбора параметров (почему именно так):

- fs = 16000 Гц, окно 1.0 с -> 16000 отсчётов. «Ева» ([j е в а])
  длится ~0.3-0.5 с, окно 1.0 с покрывает любое произношение +
  паузы вокруг слова (негативы при этом содержат речь/шум без слова).
- n_fft = 512 (32 мс): разрешение по частоте dF = fs/n_fft = 31.25 Гц.
  Этого хватает, чтобы видеть форманты гласных (F1 ~ 300-900 Гц,
  F2 ~ 1000-2500 Гц); меньшее n_fft размывает форманты, большее — теряет
  точность по времени (фонема «е»/[j] в «Ева» длится ~30-50 мс).
- hop = 160 (10 мс): фонемы длятся 30-100 мс -> по 3-10 кадров на фонему,
  свёртка kernel=5 (50 мс) "видит" целые переходы согласная->гласная.
  Кадров на окно: 1 + (16000-512)//160 = 97.
- 40 мел-бинов (64-7600 Гц). Мел-шкала имитирует восприятие уха:
  mel(f) = 2595*log10(1 + f/700) — больше бинов на низких частотах,
  где сидят форманты и голосовой тон. 40 — классика (Porcupine).
- log-mel вместо MFCC: DCT-II декоррелирует каналы, выбрасывая часть
  информации; свёрточной сети корреляция каналов не мешает (BatchNorm
  сам её снимает), а log-mel устойчивее к шуму. MFCC тоже реализован
  (mfcc=True) — для экспериментов.
- Pre-emphasis x[n] -= 0.97*x[n-1]: поднимает высокие частоты (звонкие
  согласные «ж», «в», «с»), компенсируя спад спектра голоса ~6 дБ/окт.

Инференс: STFT 97 кадров + 40 мел-бинов = ~1-3 мс на CPU в numpy.
"""

from __future__ import annotations

import wave

import numpy as np

SAMPLE_RATE = 16000
WIN_SECONDS = 1.0
WIN_SAMPLES = int(SAMPLE_RATE * WIN_SECONDS)  # 16000
N_FFT = 512
HOP = 160
N_MELS = 40
F_MIN = 64.0
F_MAX = 7600.0
PRE_EMPHASIS = 0.97

# log(10^-10): защита от log(0) (тишина даёт нули спектра)
_LOG_EPS = 1e-10


# ---------------------------------------------------------------------------
# Мел-фильтрбанк (треугольники, формула HTK)
# ---------------------------------------------------------------------------

def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def mel_filterbank(n_fft: int = N_FFT, n_mels: int = N_MELS,
                   fmin: float = F_MIN, fmax: float = F_MAX,
                   sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Матрица (n_mels, n_fft//2+1): веса треугольников на бинax БПФ."""
    n_bins = n_fft // 2 + 1
    freqs = np.linspace(0.0, sample_rate / 2.0, n_bins)
    mel_min, mel_max = float(_hz_to_mel(np.asarray([fmin]))[0]), \
        float(_hz_to_mel(np.asarray([fmax]))[0])
    mel_pts = _mel_to_hz(np.linspace(mel_min, mel_max, n_mels + 2))
    fb = np.zeros((n_mels, n_bins), dtype=np.float32)
    for m in range(n_mels):
        f_left, f_center, f_right = mel_pts[m], mel_pts[m + 1], mel_pts[m + 2]
        up = (freqs - f_left) / (f_center - f_left + 1e-9)
        down = (f_right - freqs) / (f_right - f_center + 1e-9)
        fb[m] = np.maximum(0.0, np.minimum(up, down))
    return fb


# ---------------------------------------------------------------------------
# Базовые DSP-операции
# ---------------------------------------------------------------------------

def pre_emphasis(x: np.ndarray, alpha: float = PRE_EMPHASIS) -> np.ndarray:
    """y[n] = x[n] - alpha*x[n-1]. Высокие частоты (согласные) усилены."""
    if alpha <= 0.0:
        return x
    return np.concatenate([x[:1], x[1:] - alpha * x[:-1]])


def _hann(n: int) -> np.ndarray:
    # 0.5 - 0.5*cos(2*pi*k/(N-1)) — окно Ханна: низкие боковые лепестки,
    # главный лепесток 4 бина — хороший компромисс для речи.
    k = np.arange(n, dtype=np.float32)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * k / (n - 1))


def stft_power(x: np.ndarray, n_fft: int = N_FFT, hop: int = HOP) -> np.ndarray:
    """Спектр мощности (n_fft//2+1, n_frames). Окно 1с -> 97 кадров."""
    n_frames = 1 + (len(x) - n_fft) // hop
    win = _hann(n_fft)
    # frames: (n_frames, n_fft) — вид строки, БПФ вдоль оси 1
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * win[None, :]
    spec = np.fft.rfft(frames, n=n_fft, axis=1)
    power = np.abs(spec) ** 2
    return power.T.astype(np.float32)


def log_mel(x: np.ndarray, fb: np.ndarray) -> np.ndarray:
    """(n_mels, n_frames): мел-бины -> log (сжатие динамического диапазона).
    Значения ~ -23..0; нормализация по статистикам датасета ниже."""
    return np.log10(fb @ x + _LOG_EPS).astype(np.float32)


def dct2(m: np.ndarray, n_mfcc: int = 13) -> np.ndarray:
    """DCT-II вручную (без scipy): m (n_mels, T) -> (n_mfcc, T)."""
    n_mels, n_frames = m.shape
    k = np.arange(n_mfcc)[:, None]
    n = np.arange(n_mels)[None, :]
    basis = np.cos(np.pi * k * (2.0 * n + 1.0) / (2.0 * n_mels))
    norm = np.sqrt(2.0 / n_mels)
    out = norm * (basis @ m)
    out[0] *= np.sqrt(0.5)  # c0: ортонормировка DCT-II
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Главный класс
# ---------------------------------------------------------------------------

class FeatureExtractor:
    """Сырые PCM 16k -> тензор (1, n_mels, 97) для CNN (или MFCC 13 каналов).

    Нормализация: (x - mean)/std по каждому мел-бину (статистики считаются
    на тренировочном датасете и сохраняются рядом с моделью).
    """

    def __init__(self, n_mels: int = N_MELS, mfcc: bool = False,
                 n_mfcc: int = 13):
        self.n_mels = n_mels
        self.mfcc = mfcc
        self.n_mfcc = n_mfcc
        self.fb = mel_filterbank(n_mels=n_mels)
        self.mean: np.ndarray | None = None   # (n_mels,) или (n_mfcc,)
        self.std: np.ndarray | None = None
        self._n_chan = n_mfcc if mfcc else n_mels

    # -- аудио -> признаки ------------------------------------------------
    def extract(self, x: np.ndarray) -> np.ndarray:
        """x: float32 (WIN_SAMPLES,) -> (n_chan, n_frames) без нормализации."""
        if x.shape[0] != WIN_SAMPLES:
            raise ValueError(f"окно должно быть {WIN_SAMPLES} отсчётов, "
                             f"получено {x.shape[0]}")
        y = pre_emphasis(x)
        power = stft_power(y)
        mel = log_mel(power, self.fb)
        if self.mfcc:
            return dct2(mel, self.n_mfcc)
        return mel

    def transform(self, x: np.ndarray) -> np.ndarray:
        """extract + нормализация статистиками датасета."""
        f = self.extract(x)
        if self.mean is not None:
            f = (f - self.mean[:, None]) / (self.std[:, None] + 1e-6)
        return f

    def fit_stats(self, features: np.ndarray) -> None:
        """features: (n, n_chan, n_frames) -> per-канал mean/std."""
        self.mean = features.mean(axis=(0, 2)).astype(np.float32)
        self.std = features.std(axis=(0, 2)).astype(np.float32) + 1e-6

    # -- сериализация ------------------------------------------------------
    def save_stats(self, path: str) -> None:
        np.savez(path, mean=self.mean, std=self.std,
                 n_mels=self.n_mels, mfcc=int(self.mfcc), n_mfcc=self.n_mfcc)

    @classmethod
    def load_stats(cls, path: str) -> "FeatureExtractor":
        d = np.load(path)
        fe = cls(n_mels=int(d["n_mels"]), mfcc=bool(d["mfcc"]),
                 n_mfcc=int(d["n_mfcc"]))
        fe.mean, fe.std = d["mean"].astype(np.float32), d["std"].astype(np.float32)
        return fe


# ---------------------------------------------------------------------------
# WAV: чтение/запись/ресемплинг (для TTS-датасета: 22050 Гц -> 16000)
# ---------------------------------------------------------------------------

def load_wav(path: str) -> tuple[np.ndarray, int]:
    """-> (float32 моно [-1,1], sample_rate)."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n_ch = w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if n_ch > 1:
        raw = raw[::n_ch]  # берём первый канал
    return (raw.astype(np.float32) / 32768.0), sr


def save_wav(path: str, x: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    """float32 [-1,1] -> int16 WAV."""
    pcm = np.clip(x, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def resample_linear(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Линейная интерполяция (дешёвая; для ТТS-датasета этого достаточно:
    искажение ~0.5% не критично для обучения)."""
    if sr_from == sr_to:
        return x
    n_out = int(round(len(x) * sr_to / sr_from))
    pos = np.arange(n_out) * (sr_from / sr_to)
    return np.interp(pos, np.arange(len(x), dtype=np.float64), x).astype(np.float32)


def rms(x: np.ndarray) -> float:
    """Среднеквадратичная амплитуда (для гейта и нормализации громкости)."""
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


# ---------------------------------------------------------------------------
# Синтез шума (для негативов и аугментации)
# ---------------------------------------------------------------------------

def white_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(n).astype(np.float32)


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Розовый шум (1/f): сумма случайных блужданий с вероятностью
    перезапуска 2^-k (метод Voss-McCartney). Спектр падает ~3 дБ/окт,
    как реальный комнатный шум — лучше моделирует «фон»."""
    n_octaves = 16
    out = np.zeros(n, dtype=np.float32)
    for k in range(n_octaves):
        p = 0.5 ** (k + 1)
        change = rng.random(n) < p
        delta = change.astype(np.float32) * (rng.integers(0, 2, n) * 2 - 1)
        out += np.cumsum(delta)
    return out


def brown_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Броуновский шум (1/f^2): интегрированный белый. Низкий гул."""
    out = np.cumsum(rng.standard_normal(n))
    return out.astype(np.float32)


def noise_at_rms(kind: str, n: int, target_rms: float,
                 rng: np.random.Generator) -> np.ndarray:
    """Шум нужного типа, нормализованный к заданному RMS."""
    fn = {"white": white_noise, "pink": pink_noise, "brown": brown_noise}[kind]
    x = fn(n, rng)
    cur = rms(x)
    if cur > 1e-9:
        x = x * (target_rms / cur)
    return x


def scale_to_rms(x: np.ndarray, target_rms: float) -> np.ndarray:
    cur = rms(x)
    if cur > 1e-9:
        return x * (target_rms / cur)
    return x