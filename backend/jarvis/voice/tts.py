"""Синтез речи (TTS): edge-tts (онлайн) -> RHVoice (Elena) -> Piper (irina).

Портировано из легаси-демона, включая патч rhvoice_bindings под Python 3.14
и сигнал активации (двухнотный «пик» вместо голосового «Слушаю»).
"""

import asyncio
import os
import re
import subprocess
import tempfile
import threading
import wave

import numpy as np

from jarvis import config
from jarvis.voice import audio
from jarvis.voice import wake

_speak_lock = threading.Lock()

# ============================== СИГНАЛ АКТИВАЦИИ ============================

_ATTENTION_WAV = os.path.join(tempfile.gettempdir(), 'jarvis_attention.wav')


def _make_attention_wav(path):
    """Генерирует короткий (0.25 с) двухнотный сигнал с плавным затуханием."""
    sr = 44100
    total = int(sr * 0.25)
    t = np.arange(total) / sr
    n1 = int(sr * 0.11)
    tone = np.concatenate([
        np.sin(2 * np.pi * 880.0 * t[:n1]),      # ля5
        np.sin(2 * np.pi * 1318.5 * t[n1:]),     # ми6
    ])
    n_fade = int(sr * 0.02)
    env = np.ones(total)
    env[:n_fade] = np.linspace(0, 1, n_fade, endpoint=False)
    env[-n_fade:] = np.linspace(1, 0, n_fade)
    pcm = (tone * env * 0.6 * 32767).astype('<i2')
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def play_attention_sound():
    """Короткий «пик» при активации — не перебивает мысль голосом."""
    try:
        if not os.path.isfile(_ATTENTION_WAV):
            _make_attention_wav(_ATTENTION_WAV)
        audio.play_wav(_ATTENTION_WAV)
    except Exception:
        pass


# ============================== ПОДГОТОВКА ТЕКСТА ============================

_CODE_BLOCK_RE = re.compile(
    r'```.*?```|`[^`\n]{1,80}`', re.DOTALL)
_MAX_SPOKEN_CHARS = 900


def sanitize_for_speech(text):
    """Готовит текст для синтеза: убирает эмодзи, маркдаун-мусор и
    слово-активатор «Ева» (если оно попадёт в ответ, ассистент прервёт
    собственную озвучку из-за эха микрофона). Длинные ответы сокращаем."""
    if _CODE_BLOCK_RE.search(text):
        text = _CODE_BLOCK_RE.sub(' Код готов, весь текст — в меню ассистента. ', text)
    text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+', '', text)
    text = re.sub(r'[*_#`~|>]', '', text)
    text = re.sub(r'([!?.,]){2,}', r'\1', text)
    text = re.sub(r'^\s*#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'\b(?:ева|эва)\b', 'я', text, flags=re.I)
    text = text.strip()
    if len(text) > _MAX_SPOKEN_CHARS:
        cut = text[:_MAX_SPOKEN_CHARS]
        last_stop = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
        if last_stop > _MAX_SPOKEN_CHARS * 0.5:
            cut = cut[:last_stop + 1]
        text = cut + ' Полный ответ — в меню ассистента.'
    return text


def _sentence_key(sentence):
    """Нормализованный ключ предложения для поиска дублей."""
    return re.sub(r'[\s,.!?…:;«»"()-]+', '', sentence.lower())


def _is_dup(key, seen):
    for prev in seen:
        if key == prev or (min(len(key), len(prev)) >= 8
                           and (key in prev or prev in key)):
            return True
    return False


def dedup_sentences(text):
    """Выкидывает из текста дубли и почти-дубли предложений — модели часто
    повторяют один смысл несколько раз, вслух читаем без дублей."""
    parts = re.split(r'(?<=[.!?…])(?:\s+|(?=[А-ЯЁA-Z"«]))', text)
    out = []
    seen = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        key = _sentence_key(p)
        if key and _is_dup(key, seen):
            continue
        if key:
            seen.append(key)
            if len(seen) > 8:
                seen.pop(0)
        out.append(p)
    return ' '.join(out)


# ============================== EDGE-TTS ====================================
# Нейронный онлайн-голос Microsoft (бесплатно, без API-ключа). Нужен
# интернет; при сбое демон автоматически переходит на офлайн-голоса.

def _speak_edge(text):
    """Синтезирует речь через edge-tts (web socket Microsoft Edge).
    Возвращает True, если удалось сгенерировать и проиграть. Синтез
    ограничен по времени — при зависшем соединении быстро уходим
    на офлайн-голос."""
    try:
        import edge_tts
        audio_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.mp3')

        async def _synth():
            await asyncio.wait_for(
                edge_tts.Communicate(
                    text, config.EDGE_TTS_VOICE, rate=config.EDGE_TTS_RATE,
                ).save(audio_path),
                timeout=15,
            )

        asyncio.run(_synth())
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            return False
        return audio.play_wav(audio_path)
    except Exception as exc:
        print(f'[tts] edge-tts недоступен ({exc}) — использую офлайн-голос')
        return False


# ============================== RHVOICE =====================================

_rhvoice = None
_rhvoice_lock = threading.Lock()


def _patch_rhvoice_bindings():
    """rhvoice-wrapper 0.8.0 ломается на Python 3.14: он передаёт bytes
    в ctypes.CDLL, а 3.14 принимает только str. Чиним его загрузчик."""
    try:
        import rhvoice_wrapper.rhvoice_bindings as rb

        def _selector(lib_path):
            if lib_path is None:
                return 'libRHVoice.so'
            return lib_path if isinstance(lib_path, str) else lib_path.decode()

        rb._lib_selector = _selector
        rb.load_tts_library.__globals__['_lib_selector'] = _selector
        return True
    except Exception:
        return False


def _init_rhvoice():
    """Инициализирует RHVoice (rhvoice-wrapper поверх libRHVoice + голоса).
    Возвращает False, если RHVoice недоступен — тогда используется Piper."""
    global _rhvoice
    if _rhvoice is not None:
        return _rhvoice
    with _rhvoice_lock:
        if _rhvoice is not None:
            return _rhvoice
        try:
            if not _patch_rhvoice_bindings():
                raise RuntimeError('не удалось пропатчить rhvoice_bindings')
            from rhvoice_wrapper import TTS
            kwargs = {'threads': 1}
            if os.path.isdir(config.RHVOICE_DATA_PATH):
                kwargs['data_path'] = config.RHVOICE_DATA_PATH
            tts = TTS(**kwargs)
            voices = list(tts.voices)
            if not voices:
                raise RuntimeError('не найдено ни одного голоса RHVoice')
            want = config.RHVOICE_VOICE.lower()
            voice = next((v for v in voices if v.lower() == want), None) or voices[0]
            _rhvoice = (tts, voice)
            print(f'[tts] RHVoice: голос {voice}')
        except Exception as exc:
            print(f'[tts] RHVoice недоступен ({exc}) — использую Piper (irina)')
            _rhvoice = False
    return _rhvoice


def _speak_rhvoice(text):
    pair = _init_rhvoice()
    if not pair:
        return False
    tts, voice = pair
    try:
        data = tts.get(text, voice=voice, format_='wav',
                       sets={'relative_rate': config.RHVOICE_RATE})
        if not data:
            return False
        wav_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.wav')
        with open(wav_path, 'wb') as f:
            f.write(data)
        return audio.play_wav(wav_path)
    except Exception as exc:
        print(f'[tts] ошибка RHVoice: {exc}')
        return False


# ============================== PIPER =======================================

def _piper_bin():
    """Ищет бинарь piper: в PATH и в bin/ виртуального окружения
    (venv/bin/piper не попадает в PATH systemd-демона)."""
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        candidate = os.path.join(directory, 'piper')
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import sys
    candidate = os.path.join(sys.prefix, 'bin', 'piper')
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def _speak_piper(text):
    """Синтезирует речь через Piper CLI (женский голос irina) в wav."""
    piper_bin = _piper_bin()
    if not piper_bin:
        print('[piper] бинарь "piper" не найден — установите piper-tts '
              '(pip install piper-tts)')
        return
    wav_path = os.path.join(tempfile.gettempdir(), 'jarvis_response.wav')
    cmd = [
        piper_bin,
        '--model', config.PIPER_VOICE_MODEL,
        '--output_file', wav_path,
    ]
    if config.PIPER_LENGTH_SCALE != 1.0:
        cmd += ['--length_scale', str(config.PIPER_LENGTH_SCALE)]
    try:
        subprocess.run(
            cmd,
            input=text.encode('utf-8'),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        print(f'[piper] ошибка синтеза: {exc.stderr.decode(errors="ignore")}')
        return
    except FileNotFoundError:
        print(f'[piper] бинарь "{piper_bin}" не найден — проверьте установку piper-tts')
        return
    audio.play_wav(wav_path)


# ============================== SPEAK =======================================

def speak(text):
    """Синтезирует речь и проигрывает. Сначала пробует нейронный голос
    Microsoft (edge-tts, «Светлана»), затем офлайн RHVoice (Elena),
    и в самом конце — Piper (irina)."""
    if not text:
        return
    if wake.interrupt_event.is_set():
        return
    text = sanitize_for_speech(text)
    text = dedup_sentences(text)
    if not text:
        return
    with _speak_lock:
        # защита от эха: на время речи микрофон заглушён
        audio.mic_mute_on()
        try:
            if _speak_edge(text):
                return
            if _speak_rhvoice(text):
                return
            _speak_piper(text)
        finally:
            audio.mic_mute_off()