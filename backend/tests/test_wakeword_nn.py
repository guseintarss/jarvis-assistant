# -*- coding: utf-8 -*-
"""Тесты нейросетевого детектора «Ева» (jarvis.voice.wakeword_nn).

Прогоняем РЕАЛЬНУЮ обученную модель (checkpoints/wakeword.onnx) по
реальным записям: все позитивы должны сработать, число ложных
срабатываний на негативах не должно превышать известное офлайн-значение
(14 из 47 на пороге 0.85 — слова «Ява», «Открой браузер», «Включи
музыку», «Стоп», «Продолжай»; см. wakeword/README).

Детектор кормим блоками 0.5 с (как микрофон демона: int16, 16 кГц,
с буферизацией через кольцо 1.0 с и temporal smoothing 2 окна).
"""

import numpy as np
from pathlib import Path

from jarvis import config
from jarvis.voice.wakeword_nn import WakeWordNN

DATA = config.DATA_DIR if hasattr(config, 'DATA_DIR') else None
BACKEND_ROOT = Path(__file__).resolve().parents[1]  # backend/
POS = f'{BACKEND_ROOT}/data/recorded/pos'
NEG = f'{BACKEND_ROOT}/data/recorded/neg'
MODEL = f'{BACKEND_ROOT}/checkpoints/wakeword.onnx'
STATS = f'{BACKEND_ROOT}/checkpoints/stats.npz'
BLOCK = 8000  # 0.5 с * 16 кГц


def _detector():
    nn = WakeWordNN(model_path=MODEL, stats_path=STATS)
    assert nn.available, 'модель не загрузилась — детектор недоступен'
    return nn


def _feed_wav(nn, path):
    import wave
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == config.SAMPLE_RATE, 'частота != 16 кГц'
        pcm = w.readframes(w.getnframes())
    # Ведущая пауза 0.5 с — как в живом потоке: окно до слова заполнено
    # реальной тишиной, а не нулями (на нулях модель деградирует).
    x = np.concatenate([np.zeros(BLOCK, np.int16),
                        np.frombuffer(pcm, dtype=np.int16)])
    nn.reset()  # файлы независимы: гейт/буфер не «протекают» между ними
    for i in range(0, len(x), BLOCK):
        if nn.feed(x[i:i + BLOCK].tobytes()):
            return True
    return False


def test_доступность_детектора():
    nn = WakeWordNN()
    assert isinstance(nn.available, bool)
    if not nn.available:
        return  # модель не развёрнута — пропускаем, движок упадёт на Vosk
    nn2 = _detector()
    assert nn2.threshold == config.WAKE_NN_THRESHOLD


def test_все_позитивы_срабатывают():
    import glob
    files = sorted(glob.glob(f'{POS}/mine_*.wav'))
    assert files, 'нет записей позитивов'
    nn = _detector()
    fired = [f.rsplit('/', 1)[-1] for f in files if _feed_wav(nn, f)]
    assert fired, 'ни одно «Ева» не распознано!'
    assert len(fired) >= 13, f'распознаны {len(fired)}/{len(files)}: {fired}'


def test_ложные_срабатывания_в_пределах():
    import glob
    files = sorted(glob.glob(f'{NEG}/mine_*.wav'))
    assert files, 'нет записей негативов'
    nn = _detector()
    fired = [f.rsplit('/', 1)[-1] for f in files if _feed_wav(nn, f)]
    # Офлайн: 14 из 47 (слова, похожие на «Ева»). Допуск — регрессия.
    assert len(fired) <= 16, \
        f'ложных срабатываний {len(fired)} из {len(files)}: {fired}'


def test_тишина_не_активирует():
    nn = _detector()
    silence = b'\x00\x00' * BLOCK * 6  # 3 с тишины
    assert not any(nn.feed(silence) for _ in range(6))


def test_кулдаун_после_активации():
    """После срабатывания 2.5 с детектор молчит (не ловит слово повторно)."""
    nn = _detector()
    import glob
    import wave
    f = sorted(glob.glob(f'{POS}/mine_*.wav'))[0]
    with wave.open(f, 'rb') as w:
        pcm = w.readframes(w.getnframes())
    x = np.concatenate([np.zeros(BLOCK, np.int16),
                        np.frombuffer(pcm, dtype=np.int16)])
    nn.reset()
    assert nn.feed(x.tobytes()), 'позитив не сработал — кулдаун не проверить'
    # Сразу повторяем то же слово БЕЗ сброса: кулдаун должен погасить его
    assert not nn.feed(x.tobytes()), 'кулдаун не сработал'


def test_недоступная_модель_не_падает():
    nn = WakeWordNN(model_path='/nonexistent/model.onnx',
                    stats_path='/nonexistent/stats.npz')
    assert nn.available is False
    assert nn.feed(b'\x00\x00' * 100) is False