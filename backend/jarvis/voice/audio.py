"""Аудио-слой: микрофон, кольцевой буфер, тишина, проигрывание.

Портировано из легаси-демона: boost-усиление шёпота, адаптивный порог
тишины (подстраивается под шумовой фон микрофона), mute на время озвучки
(защита от эха). Работает через sounddevice (soundfile не требуется —
поток int16, записи wav — вручную).
"""

import collections
import os
import queue
import subprocess
import threading
import wave

import numpy as np

from jarvis import config
from jarvis.voice import wake

# Очередь для потребителей (wake-распознаватель, запись команды,
# монитор прерываний).
audio_queue = queue.Queue()

# Кольцевой буфер последних AUDIO_TAIL_SECONDS аудио: команда, начатая
# в том же вдохе, что и «Ева», не потеряется при активации.
audio_tail = collections.deque(
    maxlen=max(1, int(config.AUDIO_TAIL_SECONDS * config.SAMPLE_RATE
                      / config.BLOCK_SIZE))
)

_mic_gain_smoothed = 1.0


def _boost_chunk(pcm):
    """Нормализует громкость блока: шёпот (тихие блоки) усиливает до уровня
    обычной речи, громкие не трогает. Усиление меняется плавно между блоками,
    чтобы не было щелчков и «насоса» на границе тихо/громко."""
    global _mic_gain_smoothed
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    rms_val = float(np.sqrt(np.mean(samples ** 2)))
    if rms_val > 0:
        target = min(config.MIC_GAIN_MAX, config.MIC_GAIN_TARGET_RMS / rms_val)
    else:
        target = config.MIC_GAIN_MAX
    _mic_gain_smoothed += (target - _mic_gain_smoothed) * 0.4
    samples *= _mic_gain_smoothed
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype(np.int16).tobytes()


def audio_callback(indata, frames, time_info, status):
    if status:
        print(f'[audio] {status}')
    chunk = _boost_chunk(bytes(indata))
    audio_tail.append(chunk)
    audio_queue.put(chunk)


def rms(pcm_bytes):
    data = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(data) == 0:
        return 0
    return float(np.sqrt(np.mean(data.astype(np.int32) ** 2)))


# Адаптивный порог тишины: шумовой фон подстраивается под микрофон, поэтому
# тихий голос на слабом встроенном микрофоне не теряется. Множитель 1.55 и
# абсолютный минимум 150 подобраны под тихие встроенные микрофоны.
_noise_floor = None
_speech_scale = 1.55


def is_silence(rms_value):
    """True, если rms_value — это шум, а не речь. Порог = шумовой фон * 1.55,
    но не ниже абсолютного минимума 150."""
    global _noise_floor
    if _noise_floor is None:
        _noise_floor = rms_value
    elif rms_value < _noise_floor:
        _noise_floor = rms_value                      # фон упал — мгновенно
    elif rms_value < _noise_floor * 1.5:
        _noise_floor += (rms_value - _noise_floor) * 0.05  # медленно подстраиваемся
    threshold = max(_noise_floor * _speech_scale, 150)
    return rms_value < threshold


def save_wav(path, pcm_bytes, sample_rate=config.SAMPLE_RATE):
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def drain_audio_queue():
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break


def open_mic_stream(callback):
    """Открывает поток микрофона. Возвращает stream или бросает исключение
    (модуль sounddevice импортируется здесь — голос опционален)."""
    import sounddevice as sd
    return sd.RawInputStream(
        samplerate=config.SAMPLE_RATE,
        blocksize=config.BLOCK_SIZE,
        device=config.MIC_DEVICE,
        dtype='int16',
        channels=1,
        callback=callback,
    )


# ============================== ПРОИГРЫВАНИЕ ================================

_player_proc = None


def play_wav(wav_path):
    """Проигрывает wav-файл. Порядок плееров: paplay (pulse/pipewire-pulse) →
    pw-play (pipewire) → ffplay (ffmpeg) → aplay (alsa). Возвращает True,
    если удалось воспроизвести. Процесс плеера можно прервать повторным
    словом «Ева» (см. engine.InterruptMonitor)."""
    global _player_proc
    if wake.interrupt_event.is_set():
        return False  # прервано словом «Ева» — не играть дальше
    for player in ('paplay', 'pw-play', 'ffplay', 'aplay'):
        if wake.interrupt_event.is_set():
            return False
        cmd = [player, wav_path]
        if player == 'ffplay':
            cmd = ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', wav_path]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        _player_proc = proc
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False
        finally:
            _player_proc = None
        if proc.returncode == 0:
            return True
        print(f'[player] {player} не смог воспроизвести ответ '
              f'(код {proc.returncode})')
    print('[player] ни один плеер не воспроизвёл ответ '
          '(нет paplay/pw-play/ffplay/aplay?)')
    return False


def kill_player():
    """Немедленно останавливает озвучку (вызывается из монитора прерываний)."""
    if _player_proc is not None and _player_proc.poll() is None:
        try:
            _player_proc.kill()
        except Exception:
            pass


# ============================== MUTE МИКРОФОНА ==============================
# Защита от эха: на время озвучки микрофон глушится. Не включаем обратно,
# если пользователь сам его отключил.

_mic_ctl_ok = None
_mic_was_muted = None


def _mic_probe_source():
    try:
        r = subprocess.run(
            ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
            capture_output=True, timeout=5, text=True)
        return r.stdout or ''
    except Exception:
        return ''


def mic_mute_on():
    global _mic_ctl_ok, _mic_was_muted
    if _mic_ctl_ok is None:
        try:
            probe = subprocess.run(
                ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
                capture_output=True, timeout=5, text=True)
            _mic_ctl_ok = probe.returncode == 0 and 'Mute:' in probe.stdout
        except Exception:
            _mic_ctl_ok = False
        if not _mic_ctl_ok:
            return
    _mic_was_muted = 'yes' in _mic_probe_source()
    try:
        subprocess.run(
            ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '1'],
            capture_output=True, timeout=5)
    except Exception:
        _mic_ctl_ok = False


def mic_mute_off():
    global _mic_ctl_ok, _mic_was_muted
    if _mic_was_muted is None or not _mic_ctl_ok:
        return
    try:
        if not _mic_was_muted:
            subprocess.run(
                ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '0'],
                capture_output=True, timeout=5)
    except Exception:
        _mic_ctl_ok = False
    finally:
        _mic_was_muted = None


def mic_unmute_force():
    """Размаючивает микрофон. Вызывается при старте голосового движка:
    если предыдущий экземпляр демона был убит посреди озвучки, микрофон
    мог остаться заглушённым навсегда (пользовательские mute в GUI не
    трогаем — их демон не создавал)."""
    global _mic_ctl_ok, _mic_was_muted
    try:
        probe = subprocess.run(
            ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
            capture_output=True, timeout=5, text=True)
        if probe.returncode == 0 and 'yes' in probe.stdout:
            subprocess.run(
                ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '0'],
                capture_output=True, timeout=5)
            print('[audio] микрофон был заглушён предыдущим запуском — '
                  'включён обратно')
    except Exception:
        pass
    _mic_ctl_ok = None
    _mic_was_muted = None


def _which(program):
    return (program if os.path.isfile(program) and os.access(program, os.X_OK)
            else None) or _which_path(program)


def _which_path(program):
    path = os.environ.get('PATH', '')
    for directory in path.split(os.pathsep):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None