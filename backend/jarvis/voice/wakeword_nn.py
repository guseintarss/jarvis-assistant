"""Нейросетевой детектор слова-активатора «Ева» (обученная WakeNet).

Работает ВМЕСТО текстового матчинга Vosk в цикле ожидания: модель
(ONNX int8, ~1 мс на окно) видит то же самое микрофонное аудио, что
и раньше шло в Vosk, и срабатывает по вероятности P(«Ева») > порога.

Логика копирует проверенный live-конвейер wakeword/infer.py:
    блок микрофона (0.5 с, int16) -> кольцевой буфер 1.0 с
    -> энергетический гейт (тишину модель не смотрит)
    -> P(«Ева») на скользящем окне
    -> 2 подряд окна > порога (temporal smoothing) -> активация
    -> кулдаун 2.5 с (не ловить слово повторно).

Если модель/пакет недоступны — available=False, движок продолжает
работать по-старому (Vosk + contains_wake_word).
"""

import time
from pathlib import Path

import numpy as np

from jarvis import config
from jarvis import logger


class WakeWordNN:
    """Детектор «Ева» поверх потока int16-блоков микрофона."""

    def __init__(self, model_path=None, stats_path=None, threshold=None,
                 log=None):
        self.log = log or logger.get_logger()
        self.threshold = threshold if threshold is not None \
            else config.WAKE_NN_THRESHOLD
        self._model = None
        self._ring = None
        self._gate = None
        self._prev_hit = False
        self._cooldown_until = 0.0
        self.available = False
        try:
            from wakeword import infer as wake_infer
            self._wake_infer = wake_infer
            model_path = Path(model_path or config.WAKE_MODEL_PATH)
            stats_path = Path(stats_path or config.WAKE_STATS_PATH)
            self._model = wake_infer.WakeModel(model_path, stats_path)
            self._ring = wake_infer.RingBuffer()
            self._gate = wake_infer.EnergyGate()
            self.available = True
        except Exception as exc:  # noqa: BLE001 — без NN работаем по-старому
            self.log.event('wake_nn_disabled', error=str(exc))

    def reset(self):
        """Сброс состояния (буфер/гейт/сглаживание) — между сессиями
        прослушивания. В живом потоке не нужен: гейт сам адаптируется."""
        if self._ring is not None:
            self._ring.reset()
            self._gate = self._wake_infer.EnergyGate()
            self._prev_hit = False
            self._cooldown_until = 0.0

    def feed(self, block):
        """int16-блок (0.5 с) -> True, если слово-активатор распознано.

        Блок дробится на срезы 0.1 с (как микрофон в live-конвейере):
        окно 1.0 с скользит мелкими шагами, и слово (0.3-0.5 с) проходит
        через несколько окон подряд — temporal smoothing видит его.

        Сброс состояния при активации; кулдаун не даёт ловить «Еву»
        повторно сразу после срабатывания.
        """
        if not self.available:
            return False
        if isinstance(block, bytes):
            block = np.frombuffer(block, dtype=np.int16)
        block = block.reshape(-1)
        sub = int(0.1 * config.SAMPLE_RATE)  # 1600 отсчётов
        for i in range(0, len(block), sub):
            s = block[i:i + sub]
            self._ring.push(s)
            now = time.monotonic()
            if not self._gate.speech_like(s):
                self._prev_hit = False
                continue
            if now < self._cooldown_until:
                return False
            p = self._model.score(self._ring.as_float())
            hit = p > self.threshold
            if hit and self._prev_hit:
                if config.WAKE_DEBUG:
                    print(f'[wake] NN: P(«Ева»)={p:.3f} — активация')
                self._ring.reset()
                self._prev_hit = False
                self._cooldown_until = time.monotonic() + \
                    self._wake_infer.COOLDOWN_SECONDS
                return True
            self._prev_hit = hit
        return False