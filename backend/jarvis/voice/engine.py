"""Голосовой движок демона: слово-активатор -> запись -> STT -> пайплайн -> TTS.

Портирован из легаси-демона (jarvis_daemon.py) с упрощениями: вместо
стрим-LLM — синхронный пайплайн (assistant.process), прерывание «Евой»
действует на фазу озвучки (сам вызов пайплайна атомарен).

Потоки:
  - main (GLib) — D-Bus сервис, получает команды пользователя;
  - engine — этот цикл: прослушивание «Евы», запись, обработка, озвучка;
  - InterruptMonitor — следит за микрофоном во время озвучки и прерывает
    её повторным словом-активатором.

Состояния (для расширения): idle, listening, thinking, processing,
speaking, dialog, offline.
"""

import json
import os
import queue
import re
import tempfile
import threading
import time

from jarvis import config
from jarvis.voice import audio
from jarvis.voice import stt
from jarvis.voice import tts
from jarvis.voice import wake

# Сигнал активации, произнесённый во время озвучки, прерывает её:
# после 'interrupted' движок сразу слушает новую команду.
_YES_WORDS = re.compile(r'\b(?:да|ага|конечно|выполняй|подтверждаю|давай|yes)\b')

# Минимальный RMS записи для распознавания (энергетический гейт против
# «галлюцинаций» whisper на почти-тишине). Ниже — считаем, что речи нет.
_SPEECH_RMS_MIN = 300


class InterruptMonitor:
    """Следит за микрофоном, пока демон говорит. Если пользователь снова
    произносит слово-активатор — прерывает озвучку (ставит interrupt_event
    и останавливает плеер)."""

    def __init__(self, vosk_model):
        from vosk import KaldiRecognizer
        self._rec = KaldiRecognizer(vosk_model, config.SAMPLE_RATE)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        wake.interrupt_event.clear()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = audio.audio_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            if self._rec.AcceptWaveform(chunk):
                text = json.loads(self._rec.Result()).get('text', '')
            else:
                text = json.loads(self._rec.PartialResult()).get('partial', '')
            if text and wake.contains_wake_word(text):
                print('[voice] прерывание: снова сказано слово-активатор')
                wake.interrupt_event.set()
                audio.kill_player()
                return


def record_command(initial_frames=None, max_seconds=None, hang_seconds=None):
    """Пишет аудио, пока не закончится тишина, возвращает (путь к wav, pcm).

    initial_frames — «хвост» аудио, записанный ещё ДО активации (команда,
    начатая в том же вдохе, что и «Ева», сохраняется).
    """
    frames = list(initial_frames) if initial_frames else []
    silence_seconds = 0.0
    block_seconds = config.BLOCK_SIZE / config.SAMPLE_RATE
    total_seconds = len(frames) * block_seconds
    hang = hang_seconds or config.SILENCE_HANG_SECONDS
    max_sec = max_seconds or config.MAX_COMMAND_SECONDS

    while True:
        try:
            chunk = audio.audio_queue.get(timeout=hang + 1)
        except queue.Empty:
            break
        frames.append(chunk)
        total_seconds += block_seconds
        if audio.is_silence(audio.rms(chunk)):
            silence_seconds += block_seconds
        else:
            silence_seconds = 0.0
        if silence_seconds >= hang or total_seconds >= max_sec:
            break

    pcm = b''.join(frames)
    if not pcm:
        return None, b''
    tmp_path = os.path.join(tempfile.gettempdir(), 'jarvis_command.wav')
    audio.save_wav(tmp_path, pcm)
    return tmp_path, pcm


class VoiceEngine:
    """Цикл голосового ассистента. Запускается в отдельном потоке."""

    def __init__(self):
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._manual_activation = threading.Event()
        self._mode = config.ACTIVATION_MODE
        self._available = False
        self._confirm_owner = False
        self._state = 'idle'
        self._state_lock = threading.Lock()
        # обратные вызовы (выставляет D-Bus сервис)
        self.on_state = None      # on_state(state: str)
        self.on_heard = None      # on_heard(text: str)
        self.on_response = None   # on_response(text: str)
        self.process_fn = None    # process_fn(text) -> dict  (пайплайн)

    # --------------------------- публичный API -----------------------------

    @property
    def available(self):
        return self._available

    @property
    def state(self):
        with self._state_lock:
            return self._state

    @property
    def paused(self):
        return self._paused.is_set()

    @property
    def mode(self):
        return self._mode

    def attach(self, process_fn, on_state, on_heard, on_response):
        self.process_fn = process_fn
        self.on_state = on_state
        self.on_heard = on_heard
        self.on_response = on_response

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def toggle_pause(self):
        if self._paused.is_set():
            self._paused.clear()
        else:
            self._paused.set()

    def interrupt(self):
        """Прерывание (кнопка в расширении / «Ева» во время озвучки)."""
        wake.interrupt_event.set()
        audio.kill_player()

    def manual_activate(self):
        self._manual_activation.set()

    def set_mode(self, mode):
        if mode in ('voice', 'hotkey', 'both'):
            self._mode = mode

    # --------------------------- состояние ---------------------------------

    def _set_state(self, new_state):
        with self._state_lock:
            if new_state == self._state:
                return
            self._state = new_state
        if self.on_state is not None:
            try:
                self.on_state(new_state)
            except Exception as exc:
                print(f'[voice] ошибка on_state: {exc}')

    # --------------------------- подтверждение ------------------------------

    def confirm_voice(self, question):
        """Голосовое подтверждение опасного действия (prompt_fn для
        Confirmator). Работает только внутри голосового потока: текстовые
        команды по D-Bus без голосового цикла получают безопасный отказ."""
        if not self._available or not self._confirm_owner:
            return 'n'
        try:
            q = re.sub(r'\s*\[y/N\]\s*$', '', question).strip()
            tts.speak(f'{q} Скажите да или нет.')
            wav_path, pcm = record_command(max_seconds=4, hang_seconds=1.2)
            if not wav_path or audio.rms(pcm) < _SPEECH_RMS_MIN:
                print('[voice] подтверждение: не расслышал ответ')
                return 'n'
            text = stt.transcribe(wav_path, stt.get_whisper_model())
            text = wake.strip_wake_word(text)
            print(f'[voice] подтверждение: {text!r}')
            return 'y' if _YES_WORDS.search(text.lower()) else 'n'
        except Exception as exc:
            print(f'[voice] ошибка подтверждения: {exc}')
            return 'n'

    # --------------------------- главный цикл -------------------------------

    def _run(self):
        # На слабом железе даём GNOME Shell спокойно стартовать после логина.
        time.sleep(config.STARTUP_DELAY_SECONDS)

        # Если предыдущий экземпляр демона был убит посреди озвучки, он мог
        # оставить микрофон заглушённым — возвращаем его в рабочее состояние.
        audio.mic_unmute_force()

        try:
            from vosk import Model, KaldiRecognizer
            vosk_model = Model(config.VOSK_MODEL_PATH)
            recognizer = KaldiRecognizer(vosk_model, config.SAMPLE_RATE)
            self._vosk_model = vosk_model
        except Exception as exc:
            print(f'[voice] НЕ удалось загрузить Vosk-модель: {exc}')
            print(f'[voice] Проверьте, что install.sh --with-voice скачал '
                  f'модель в {config.VOSK_MODEL_PATH}.')
            self._set_state('offline')
            return  # D-Bus остаётся жить — демон работает текстом

        try:
            stream = audio.open_mic_stream(audio.audio_callback)
        except Exception as exc:
            print(f'[voice] НЕ удалось открыть микрофон: {exc}')
            print('[voice] Проверьте звук (pactl info) и настройку MIC_DEVICE.')
            self._set_state('offline')
            return

        self._available = True
        mode_hint = {
            'voice': 'Говорите "Ева" для активации.',
            'hotkey': 'Режим: активация только по горячей клавише.',
            'both': 'Говорите "Ева" или нажмите горячую клавишу.',
        }
        print(f'[voice] готов. {mode_hint.get(self._mode, "")}')
        self._set_state('idle')

        with stream:
            while not self._stop.is_set():
                try:
                    if not self._wake_listen(recognizer):
                        continue
                except Exception as exc:
                    print(f'[voice] ошибка в цикле прослушивания: {exc}')
                    continue

                recognizer.Reset()

                # После прерывания («Ева» во время ответа) слово уже сказано —
                # сразу слушаем новую команду, без повторной активации.
                status = 'done'
                while not self._stop.is_set():
                    try:
                        status = self._run_command_flow()
                    except Exception as exc:
                        print(f'[voice] ошибка в обработке команды: {exc}')
                        status = 'done'
                    if status != 'interrupted':
                        break
                    print('[voice] слушаю новую команду...')
                recognizer.Reset()

                # Непрерывный диалог: после ответа слушаем уточнения БЕЗ
                # слова-активатора, пока пользователь говорит.
                if (status == 'done' and config.DIALOGUE_MODE_ENABLED
                        and self._mode != 'hotkey'):
                    print('[voice] диалоговый режим: слушаю уточнения без «Ева»...')
                    try:
                        while not self._stop.is_set():
                            if not self._dialogue_listen(recognizer):
                                break
                            recognizer.Reset()
                            try:
                                status = self._run_command_flow(dialogue=True)
                            except Exception as exc:
                                print(f'[voice] ошибка в диалоге: {exc}')
                                status = 'done'
                            recognizer.Reset()
                            if status != 'done':
                                break
                    except Exception as exc:
                        print(f'[voice] ошибка в диалоговом режиме: {exc}')
                    print('[voice] возврат к ожиданию «Ева»')

    # --------------------------- прослушивание ------------------------------

    def _wake_listen(self, recognizer):
        """Ждёт слово-активатор (или ручную активацию). Возвращает True,
        если пора слушать команду."""
        while not self._stop.is_set():
            stt.unload_whisper_if_idle()

            if self._paused.is_set():
                try:
                    audio.audio_queue.get(timeout=0.5)
                except queue.Empty:
                    pass
                if self._manual_activation.is_set():
                    self._manual_activation.clear()
                continue

            if self._manual_activation.is_set():
                self._manual_activation.clear()
                return True

            if self._mode == 'hotkey':
                # Голос не слушаем, ждём только ручную активацию.
                if self._manual_activation.wait(0.5):
                    self._manual_activation.clear()
                    return True
                try:
                    audio.audio_queue.get_nowait()
                except queue.Empty:
                    pass
                continue

            try:
                chunk = audio.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if recognizer.AcceptWaveform(chunk):
                text = json.loads(recognizer.Result()).get('text', '')
            else:
                text = json.loads(recognizer.PartialResult()).get('partial', '')

            if config.WAKE_DEBUG and text:
                print(f'[wake] слышу: {text!r}')

            if not wake.contains_wake_word(text):
                continue

            recognizer.Reset()
            return True

        return False

    def _dialogue_listen(self, recognizer):
        """Режим непрерывного диалога (как у Алисы): после ответа слушаем
        следующую реплику БЕЗ слова-активатора.

        Возвращает True, если реплика услышана; False — если диалог пора
        заканчивать: наступила тишина, нажата пауза, сказано одно «Ева»
        либо остановлен сервис.
        """
        # Защита от эха: после озвучки собственный голос Евы ещё несколько
        # мгновений идёт с колонок в микрофон. Начало фразы пользователя,
        # сказанной в этот момент, сохраняет кольцевой буфер audio_tail.
        guard_end = time.monotonic() + config.DIALOGUE_ECHO_GUARD_SECONDS
        while not self._stop.is_set() and time.monotonic() < guard_end:
            try:
                audio.audio_queue.get(timeout=0.1)
            except queue.Empty:
                break

        recognizer.Reset()
        deadline = time.monotonic() + config.DIALOGUE_TIMEOUT_SECONDS
        block_seconds = config.BLOCK_SIZE / config.SAMPLE_RATE
        min_speech_blocks = max(1, round(
            config.DIALOGUE_MIN_SPEECH_SECONDS / block_seconds))
        speech_blocks = 0
        self._set_state('dialog')
        while not self._stop.is_set():
            if self._paused.is_set():
                self._set_state('idle')
                return False
            if self._manual_activation.is_set():
                self._manual_activation.clear()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._set_state('idle')
                return False
            try:
                chunk = audio.audio_queue.get(timeout=min(0.3, remaining))
            except queue.Empty:
                continue
            # Репликой считается только «живая» речь: непрерывный не-тихий
            # звук длительностью DIALOGUE_MIN_SPEECH_SECONDS + текст.
            if audio.is_silence(audio.rms(chunk)):
                speech_blocks = 0
            else:
                speech_blocks += 1
            if recognizer.AcceptWaveform(chunk):
                text = json.loads(recognizer.Result()).get('text', '')
                if text and speech_blocks >= min_speech_blocks:
                    if wake.contains_wake_word(text) and not _strip_other_words(text):
                        # Сказано только слово-активатор («Ева» и пауза) —
                        # это сигнал «диалог окончен»: выходим тихо.
                        self._set_state('idle')
                        return False
                    self._set_state('idle')
                    return True
        self._set_state('idle')
        return False

    # --------------------------- команда ------------------------------------

    def _run_command_flow(self, dialogue=False):
        """Полный цикл одной команды: запись -> распознавание -> пайплайн
        -> озвучка.

        dialogue — True, если это продолжение разговора в диалоговом режиме.

        Возвращает:
          'done'        — команда обработана;
          'empty'       — команда не распознана (возврат к ожиданию слова);
          'interrupted' — во время озвучки снова сказано «Ева».
        """
        print('[voice] активация, слушаю команду...')
        self._set_state('listening')

        # Сигнал активации не нужен в hotkey-режиме (пользователь сам
        # активировал клавишей) и в диалоговом (это продолжение разговора).
        if self._mode != 'hotkey' and not dialogue:
            tts.play_attention_sound()

        # Очередь очищаем от «хвоста» (команду записываем заново), но не
        # трогаем кольцевой буфер audio_tail: если пользователь начал фразу
        # в том же вдохе, что и «Ева», её начало останется в буфере.
        audio.drain_audio_queue()

        command_text = ''
        for attempt in range(config.RECORD_RETRIES + 1):
            try:
                # Хвост берём только на первой попытке: при переспросе он
                # может содержать эхо нашей же озвучки «Не расслышал…».
                wav_path, pcm = record_command(
                    initial_frames=(list(audio.audio_tail) if attempt == 0
                                    else None))
                self._set_state('thinking')
                # Энергетический гейт: whisper «галлюцинирует» свой
                # подсказочный промпт на почти-тихих записях («Сделай
                # громче, тише…»), а это может выполнить фантомную команду.
                # Если речи нет — не распознаём вовсе.
                if wav_path and audio.rms(pcm) >= _SPEECH_RMS_MIN:
                    command_text = stt.transcribe(wav_path,
                                                  stt.get_whisper_model())
                else:
                    command_text = ''
            except Exception as exc:
                print(f'[voice] ошибка записи/распознавания: {exc}')
                command_text = ''

            command_text = wake.strip_wake_word(command_text)
            print(f'[voice] распознано: {command_text!r}')

            if command_text:
                break

            # В диалоговом режиме не переспрашиваем: триггером мог стать
            # фоновый шум, и голосовое «Не расслышал» — это ответ на шум.
            if dialogue:
                break

            if attempt < config.RECORD_RETRIES:
                print('[voice] не расслышал, переспрашиваю...')
                self._set_state('listening')
                try:
                    tts.speak('Не расслышал. Повторите, пожалуйста.')
                except Exception:
                    pass
                audio.drain_audio_queue()

        if not command_text:
            self._set_state('idle')
            return 'empty'

        # Расширение показывает услышанную фразу на «острове».
        if self.on_heard is not None:
            try:
                self.on_heard(command_text)
            except Exception as exc:
                print(f'[voice] ошибка on_heard: {exc}')

        # Пайплайн (локальный MLP / облачный LLM / инструменты). Может
        # запросить голосовое подтверждение опасного действия.
        result = {'response': ''}
        if self.process_fn is not None:
            self._confirm_owner = True
            try:
                result = self.process_fn(command_text)
            finally:
                self._confirm_owner = False

        response = (result or {}).get('response') or 'Готово.'
        if self.on_response is not None:
            try:
                self.on_response(response)
            except Exception as exc:
                print(f'[voice] ошибка on_response: {exc}')

        # Озвучка, прерываемая словом «Ева» (не в hotkey-режиме).
        monitor = None
        if self._mode != 'hotkey':
            monitor = InterruptMonitor(self._vosk_model)
            monitor.start()
        self._set_state('speaking')
        try:
            tts.speak(response)
        except Exception as exc:
            print(f'[voice] ошибка озвучки: {exc}')
        if monitor is not None:
            monitor.stop()

        if wake.interrupt_event.is_set():
            wake.interrupt_event.clear()
            self._set_state('idle')
            return 'interrupted'

        self._set_state('idle')
        return 'done'


def _strip_other_words(text):
    """True, если в тексте кроме слова-активатора есть другие слова."""
    return any(t not in ('ева', 'эва') and t
               for t in re.findall(r'[а-яё]+', text.lower()))