# -*- coding: utf-8 -*-
"""ШАГ 5. Непрерывное прослушивание микрофона в реальном времени.

Конвейер:
  sounddevice RawInputStream (int16, 16 кГц, блоки 100 мс)
    -> кольцевой буфер 1.0 с (16000 отсчётов)
    -> энергетический гейт (RMS-адаптивный шумовой пол) — CPU экономится,
       когда в комнате тихо (модель НЕ запускается на тишине)
    -> log-mel (numpy, ~1-3 мс) -> WakeNet (ONNX int8, ~1 мс)
    -> P(«Ева») > 0.85 -> "✅ Ева активирована!" + сброс буфера.

Задержка: слово детектируется в среднем через ~0.3-0.5 с после его начала
(окно 1.0 с + каденция 100 мс), CPU при этом свободен ~97% времени.

Особенности Linux:
  - звук идёт через PortAudio (sounddevice): нужен пакет portaudio
    (Arch: pacman -S portaudio; Ubuntu: libportaudio2) и рабочий
    PipeWire/PulseAudio (pactl info — проверить);
  - --device 0..N или имя устройства (sounddevice.query_devices());
  - если микрофон занят другим процессом (наш демон!), PortAudio
    бросит PortAudioError: Error -9996 — выберите другой источник;
  - громкость: pactl set-source-volume @DEFAULT_SOURCE@ 100%.

Использование:
    python -m wakeword.infer --model checkpoints/wakeword.onnx --threshold 0.85
    python -m wakeword.infer --list                 # список устройств
    python -m wakeword.infer --test-wav pos_0000.wav --model ... # офлайн-тест
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np

from .features import (SAMPLE_RATE, WIN_SAMPLES, FeatureExtractor, load_wav,
                       rms)

# Если есть onnxruntime — инференс int8; иначе нужен torch (только для теста)
try:
    import onnxruntime as ort
    _HAS_ORT = True
except ImportError:
    _HAS_ORT = False
try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

import sounddevice as sd

BLOCK_SIZE = int(0.1 * SAMPLE_RATE)     # 100 мс — блок микрофона
COOLDOWN_SECONDS = 2.5                  # после срабатывания слушаем молча
GATE_HYSTERESIS = 2.0                   # RMS > пол*2 -> «есть речь»
GATE_MIN_RMS = 40.0                     # абсолютный минимум (отсчёты int16)
DEFAULT_THRESHOLD = 0.85


class RingBuffer:
    """Скользящее окно 1.0 с: новые отсчёты дописываются, старые уходят."""

    def __init__(self, size: int = WIN_SAMPLES):
        self.size = size
        self.data = np.zeros(size, dtype=np.int16)

    def push(self, block: np.ndarray) -> None:
        n = len(block)
        if n >= self.size:
            self.data[:] = block[-self.size:]
            return
        self.data = np.roll(self.data, -n)
        self.data[-n:] = block

    def reset(self) -> None:
        self.data.fill(0)

    def as_float(self) -> np.ndarray:
        """int16 -> float32 [-1, 1] (без копии не выйдет — int16 не делится)."""
        return self.data.astype(np.float32) / 32768.0


class EnergyGate:
    """Адаптивный шумовой пол: гейт открывается, когда RMS выше пола*2.
    Пол медленно следует за фоном (0.9999 — постоянная ~27 мин)."""

    def __init__(self, min_rms: float = GATE_MIN_RMS,
                 hysteresis: float = GATE_HYSTERESIS):
        self.min_rms = min_rms
        self.hyst = hysteresis
        self.floor = min_rms

    def speech_like(self, block: np.ndarray) -> bool:
        cur = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        if cur > max(self.floor * self.hyst, self.min_rms):
            self.floor = max(self.min_rms, self.floor * 0.9999 + cur * 0.0001)
            return True
        self.floor = max(self.min_rms, self.floor * 0.9999 + cur * 0.0001)
        return False


class WakeModel:
    """Обёртка над ONNX int8 (или torch-запасной). Вход (1, 40, 97)."""

    def __init__(self, path: Path, stats: Path):
        self.n_mels = 40
        self._fe = FeatureExtractor.load_stats(str(stats))
        if _HAS_ORT and path.suffix == ".onnx":
            self._sess = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"])
            self._name = self._sess.get_inputs()[0].name
            self._backend = "onnx"
        elif _HAS_TORCH:
            from .model import WakeNet
            self._torch = WakeNet(n_mels=self._fe.n_mels)
            ck = torch.load(path, map_location="cpu", weights_only=False)
            self._torch.load_state_dict(
                ck.get("state_dict", ck))  # .pt или голый state_dict
            self._torch.eval()
            self._backend = "torch"
        else:
            raise SystemExit("Нужен onnxruntime или torch для инференса")

    def score(self, window: np.ndarray) -> float:
        """float32-окно 1.0 с -> P(«Ева»)."""
        f = self._fe.transform(window)          # (40, 97) норм.
        x = f[None, None, ...]                  # (1, 1, 40, 97)
        if self._backend == "onnx":
            logit = self._sess.run(None, {self._name: x})[0]
            return float(1.0 / (1.0 + np.exp(-logit[0][0])))
        with torch.no_grad():
            return float(torch.sigmoid(self._torch(torch.from_numpy(x))).item())


def print_devices() -> None:
    print("Устройства ввода (микрофоны):")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i}: {d['name']}  (input)  "
                  f"default_samplerate={d['default_samplerate']}")


def live_loop(model: WakeModel, device, threshold: float,
              debug: bool = False) -> None:
    q: queue.Queue = queue.Queue(maxsize=8)
    ring, gate = RingBuffer(), EnergyGate()
    cooldown_until = 0.0
    prev_hit = False  # temporal smoothing: срабатываем на 2-м подряд окне

    def callback(indata, frames, t, status):  # поток PortAudio
        if status:
            print(f"    [status] {status}", file=sys.stderr)
        try:
            q.put_nowait(indata.copy())
        except queue.Full:
            pass  # переполнение: главный цикл не успевает — пропускаем

    print(f"==> Слушаю микрофон (устройство: {device!r}), порог "
          f"{threshold:.2f}. Скажите «Ева».")
    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                               device=device, dtype="int16", channels=1,
                               callback=callback):
            while True:
                try:
                    block = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                block = block.reshape(-1)
                ring.push(block)
                now = time.monotonic()
                # 1) Энергетический гейт: тишину пропускаем без модели
                if not gate.speech_like(block):
                    prev_hit = False
                    continue
                # 2) Кулдаун после срабатывания (не ловить слово повторно)
                if now < cooldown_until:
                    continue
                # 3) Прогон модели по скользящему окну
                t0 = time.perf_counter()
                p = model.score(ring.as_float())
                dt_ms = (time.perf_counter() - t0) * 1e3
                hit = p > threshold
                # 4) Temporal smoothing: одиночный всплеск p на одном окне —
                #    почти всегда артефакт; требуем 2 подряд (>200 мс).
                if hit and prev_hit:
                    print("✅ Ева активирована!")
                    ring.reset()
                    cooldown_until = time.monotonic() + COOLDOWN_SECONDS
                    prev_hit = False
                else:
                    prev_hit = hit
                if debug:
                    print(f"    p={p:.3f} ({dt_ms:.1f} мс, RMS "
                          f"{rms(ring.as_float()):.4f}, hit={int(hit)})")
    except sd.PortAudioError as exc:
        raise SystemExit(
            f"Ошибка микрофона: {exc}\n"
            f"  Проверьте: pactl info (PipeWire/Pulse жив?), "
            f"и что микрофон не занят другим процессом "
            f"(например, демоном jarvis). --list — список устройств.")


def test_wav(model: WakeModel, path: Path, threshold: float) -> None:
    """Офлайн-тест: скользящее окно по wav-файлу, печать вероятностей."""
    x, sr = load_wav(str(path))
    if sr != SAMPLE_RATE:
        from .features import resample_linear
        x = resample_linear(x, sr, SAMPLE_RATE)
    hits = 0
    for start in range(0, len(x) - WIN_SAMPLES, BLOCK_SIZE):
        win = x[start:start + WIN_SAMPLES]
        p = model.score(win)
        mark = " <== ЕВА" if p > threshold else ""
        print(f"    t={start / SAMPLE_RATE:5.2f}s  p={p:.3f}{mark}")
        hits += p > threshold
    print(f"==> {hits} срабатываний на {path.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Wake-word инференс")
    ap.add_argument("--model", default="checkpoints/wakeword.onnx")
    ap.add_argument("--stats", default="checkpoints/stats.npz")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--device", default=None, help="индекс или имя микрофона")
    ap.add_argument("--list", action="store_true", help="список устройств")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test-wav", default=None, help="офлайн-прогон по файлу")
    args = ap.parse_args()

    if args.list:
        print_devices()
        return

    model = WakeModel(Path(args.model), Path(args.stats))
    if args.test_wav:
        test_wav(model, Path(args.test_wav), args.threshold)
    else:
        live_loop(model, args.device, args.threshold, args.debug)


if __name__ == "__main__":
    main()