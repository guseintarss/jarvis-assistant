# -*- coding: utf-8 -*-
"""ШАГ 3. Архитектура: WakeNet — крошечная 1D-CNN по времени.

Почему 1D-CNN (свёртка вдоль оси времени, каналы = 40 мел-бинов):
- 2D-CNN по (частоты x время) обучается дольше и требует ~10x параметров
  при той же ёмкости; для бинарного детектора слова этого не нужно.
- Conv1d kernel=5 (50 мс) по 97 кадрам извлекает локальные спектрально-
  временные паттерны (переходы [j]->[е], [е]->[в], [в]->[а] в «Ева»),
  а Global Average Pooling в конце усредняет свидетельства по всей длине
  окна -> позиционная инвариантность (слово может начинаться в любом месте
  окна — сеть не переобучается на «слово всегда в начале»).

Слои и число параметров (вход 1 x 40 x 97):
  Conv1d(40->16, k=5) + BN + ReLU + MaxPool2    -> 40*5*16+16    = 3216
  Conv1d(16->32, k=5) + BN + ReLU + MaxPool2    -> 16*5*32+32    = 2592
  Conv1d(32->48, k=5) + BN + ReLU + MaxPool2    -> 32*5*48+48    = 7728
  GAP(48) -> Dropout(0.3) -> Linear(48->1)      -> 48+1          = 49
  ИТОГО ~13.6K параметров = 54 КБ fp32, ~14 КБ int8 (ONNX) — << 500 КБ.

После 3 MaxPool(2) время сжимается 97 -> 48 -> 24 -> 12 кадров; каждый
агрегат GAP покрывает ~8 кадров (80 мс) — одна фонема. Инференс int8
на слабом CPU: ~0.5-2 мс — реальное время при блоке микрофона 100 мс.

Выход: 1 логит -> sigmoid -> P(«Ева»). BCEWithLogitsLoss на этапе 4.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn


class WakeNet(nn.Module):
    def __init__(self, n_mels: int = 40, channels=(16, 32, 48),
                 kernel: int = 5, dropout: float = 0.3):
        super().__init__()
        blocks = []
        in_ch = n_mels
        for out_ch in channels:
            blocks += [
                nn.Conv1d(in_ch, out_ch, kernel, padding=kernel // 2),
                nn.BatchNorm1d(out_ch),
                nn.ReLU(inplace=True),
                nn.MaxPool1d(2),
            ]
            in_ch = out_ch
        self.backbone = nn.Sequential(*blocks)
        # Global Average Pooling по времени (AdaptiveAvgPool1d(1)).
        # ВАЖНО: новый экспортёр torch 2.13 (dynamo=True) разворачивает
        # его в ReduceMean с осью-инпутом — ломает shape-inference и
        # int8-квантование onnxruntime; поэтому экспорт идёт через
        # легаси-экспортёр (dynamo=False, см. export_onnx).
        self.pool = nn.AdaptiveAvgPool1d(1)
        # GAP по времени -> вектор (B, 48) -> 1 логит
        self.head = nn.Sequential(
            nn.Dropout(p=dropout),
            nn.Linear(channels[-1], 1),
        )
        # честный подсчёт параметров при создании
        self._n_params = sum(p.numel() for p in self.parameters())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (B, 1, n_mels, T) -> (B, 1)."""
        x = x.squeeze(1)              # (B, n_mels, T) — время = ось 2
        h = self.backbone(x)          # (B, 48, T/8)
        h = self.pool(h).squeeze(2)   # Global Average Pooling
        return self.head(h)           # (B, 1) — логит

    def probability(self, x: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward(x))

    @property
    def n_params(self) -> int:
        return self._n_params


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def export_onnx(model: nn.Module, path: str, n_mels: int = 40,
                n_frames: int = 97, quantize: bool = True) -> str:
    """Экспорт в ONNX (+динамическое квантование Conv/Linear в int8).

    Динамическое квантование: веса -> int8 (сдвиг/масштаб на слой),
    активации остаются float32. Потери точности ~0.5-1%, выигрыш: модель
    ~54 КБ -> ~14 КБ и x2-3 ускорения на CPU (int8 AVX2).

    Статическая форма (1, 40, 97): wake-word детектор всегда работает
    с batch=1 и окном 1.0 с; статический граф проще квантовать и он
    быстрее (без динамических reshape).

    dynamo=False (легаси-экспортёр): новый экспортёр torch 2.13 кладёт
    оси ReduceMean/Squeeze как обычные входы, а не константы, из-за чего
    падает onnx.shape_inference и quantize_dynamic; легаси-экспортёр
    генерирует константы-инициализаторы и всё квантуется.
    """
    model.eval()
    x = torch.randn(1, 1, n_mels, n_frames)
    with torch.no_grad():
        torch.onnx.export(
            model, x, path,
            input_names=["input"], output_names=["logit"],
            opset_version=17, dynamo=False,
        )
    if quantize:
        try:
            # Стандартный путь: динамическое квантование через onnxruntime
            # (QDQ-формат, веса int8, активации float32) — torch 2.13+
            # больше не имеет torch.onnx.load/save для этой задачи.
            from onnxruntime.quantization import QuantType, quantize_dynamic
            tmp = str(path) + ".tmp"
            quantize_dynamic(str(path), tmp, weight_type=QuantType.QInt8)
            Path(tmp).replace(path)
        except Exception as exc:  # квантование опционально
            print(f"    [!] int8-квантование не удалось ({exc}), "
                  f"оставляю fp32")
    return path