"""Распознавание речи (STT): faster-whisper, ленивая загрузка.

Модель грузится только при первой команде и выгружается при длительном
простое (освобождает ~270 МБ ОЗУ) — портировано из легаси-демона.
"""

import threading
import time

from jarvis import config

_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_last_used = 0.0


def get_whisper_model():
    """faster-whisper грузится лениво — только при первой активации,
    чтобы в фоне (в том числе сразу после логина) демон не жрал RAM/CPU."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                _whisper_model = WhisperModel(
                    config.WHISPER_MODEL_SIZE,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                    cpu_threads=config.WHISPER_CPU_THREADS,
                    num_workers=1,
                )
                print(f'[voice] faster-whisper ({config.WHISPER_MODEL_SIZE}, '
                      'int8) загружен')
    return _whisper_model


def unload_whisper_if_idle():
    """Если команды давно не было — выгружаем faster-whisper из памяти."""
    global _whisper_model
    if _whisper_model is None or _whisper_last_used == 0:
        return
    if time.monotonic() - _whisper_last_used <= config.WHISPER_UNLOAD_IDLE_SECONDS:
        return
    print(f'[voice] faster-whisper выгружен (простой > '
          f'{config.WHISPER_UNLOAD_IDLE_SECONDS // 60} мин)')
    _whisper_model = None


def transcribe(wav_path, whisper_model):
    """Распознаёт wav-файл, возвращает текст."""
    global _whisper_last_used
    _whisper_last_used = time.monotonic()
    segments, _info = whisper_model.transcribe(
        wav_path,
        language='ru',
        beam_size=config.WHISPER_BEAM_SIZE,
        vad_filter=True,               # пропускает тишину — быстрее и меньше «галлюцинаций»
        # VAD по умолчанию срезает тихое начало/конец фразы (400 мс запаса) —
        # для коротких команд на слабом микрофоне это теряет первые слова.
        vad_parameters={
            # Порог 0.35 вместо 0.5 по умолчанию: шёпот не вырезается VAD.
            'threshold': 0.35,
            'min_silence_duration_ms': 800,
            'speech_pad_ms': 600,
            'min_speech_duration_ms': 150,
        },
        condition_on_previous_text=False,  # короткие команды — без зацикливаний
        initial_prompt=config.WHISPER_INITIAL_PROMPT,
    )
    return ' '.join(seg.text.strip() for seg in segments).strip()