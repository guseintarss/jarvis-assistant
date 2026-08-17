"""Тесты голосового пайплайна демона Евы (backend/jarvis_daemon.py).

Запуск (из каталога backend/):
    ../.local/share/jarvis-assistant/venv/bin/python -m unittest \
        tests.legacy_test_daemon -v

Тесты роутера подменяют моками функции действий (action_*), чтобы не менять
реальную громкость/яркость/окна и не открывать приложения во время прогона.
"""

import os
import sys
import unittest
import urllib.parse
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import numpy as np  # noqa: E402

import jarvis_daemon as d  # noqa: E402


class TestWakeWord(unittest.TestCase):
    """Слово-активатор: точное, фонетические варианты, без ложных срабатываний."""

    def test_точное_слово(self):
        for w in ('ева', 'эва'):
            self.assertTrue(d.contains_wake_word(w))

    def test_в_фразе(self):
        self.assertTrue(d.contains_wake_word('ева открой браузер'))

    def test_фонетические_варианты(self):
        # Маленькая Vosk-модель часто пишет «ево», «еву», «йева»
        for w in ('ево', 'еву', 'эво', 'йева', 'ъева'):
            self.assertTrue(d.contains_wake_word(w))

    def test_нет_ложных_срабатываний(self):
        # «дева», «нева», «лева» — «ева» лишь буквально внутри слова
        for w in ('дева', 'нева', 'лева', 'поле', 'рева'):
            self.assertFalse(d.contains_wake_word(w))

    def test_пустой_текст(self):
        self.assertFalse(d.contains_wake_word(''))


class TestStripWakeWord(unittest.TestCase):
    def test_убирает_слово_из_начала(self):
        self.assertEqual(d.strip_wake_word('ева открой браузер'),
                         'открой браузер')

    def test_убирает_с_запятой(self):
        self.assertEqual(d.strip_wake_word('Ева, открой браузер'),
                         'открой браузер')

    def test_фонетический_вариант(self):
        self.assertEqual(d.strip_wake_word('ево открой'), 'открой')

    def test_без_слова_активатора(self):
        self.assertEqual(d.strip_wake_word('привет'), 'привет')

    def test_не_трогает_деву(self):
        self.assertEqual(d.strip_wake_word('дева'), 'дева')


class TestFastRouter(unittest.TestCase):
    """Быстрый роутер: regex → действие. Действия мокаются."""

    def test_не_распознанная_команда_возвращает_none(self):
        self.assertIsNone(d.find_fast_route('расскажи про космос'))

    def test_пустая_команда(self):
        self.assertIsNone(d.find_fast_route(''))
        self.assertIsNone(d.find_fast_route('   '))

    def test_выключенные_системные_действия(self):
        with mock.patch.object(d, 'ALLOW_SYSTEM_ACTIONS', False):
            self.assertIsNone(d.find_fast_route('громкость на 50'))

    # --- громкость / звук ---

    def test_громкость_абсолютная(self):
        with mock.patch.object(d, 'action_set_volume',
                               return_value='ОК') as act:
            self.assertEqual(d.find_fast_route('громкость на 50'), 'ОК')
            act.assert_called_once_with({'percent': 50})

    def test_громче(self):
        with mock.patch.object(d, 'action_adjust_volume') as act:
            self.assertEqual(d.find_fast_route('сделай громче'),
                             'Сделал громче.')
            act.assert_called_once_with('+10%')

    def test_тише(self):
        with mock.patch.object(d, 'action_adjust_volume') as act:
            self.assertEqual(d.find_fast_route('потише'), 'Сделал тише.')
            act.assert_called_once_with('-10%')

    def test_выключить_звук(self):
        with mock.patch.object(d, 'action_set_mute') as act:
            d.find_fast_route('выключи звук')
            act.assert_called_once_with({'mute': True})

    def test_включить_звук(self):
        with mock.patch.object(d, 'action_set_mute') as act:
            d.find_fast_route('включи звук')
            act.assert_called_once_with({'mute': False})

    # --- яркость ---

    def test_яркость_абсолютная(self):
        with mock.patch.object(d, 'action_set_brightness') as act:
            d.find_fast_route('яркость 30 процентов')
            act.assert_called_once_with({'percent': 30})

    def test_ярче(self):
        with mock.patch.object(d, 'action_adjust_brightness') as act:
            self.assertEqual(d.find_fast_route('ярче'), 'Сделал ярче.')
            act.assert_called_once_with('10%+')

    def test_темнее(self):
        with mock.patch.object(d, 'action_adjust_brightness') as act:
            self.assertEqual(d.find_fast_route('сделай темнее'),
                             'Сделал темнее.')
            act.assert_called_once_with('10%-')

    # --- wi-fi / тема / ночной режим / блокировка / сон ---

    def test_включить_вайфай(self):
        with mock.patch.object(d, 'action_set_wifi') as act:
            d.find_fast_route('включи вай-фай')
            act.assert_called_once_with({'state': 'on'})

    def test_выключить_wifi(self):
        with mock.patch.object(d, 'action_set_wifi') as act:
            d.find_fast_route('выключи wi-fi')
            act.assert_called_once_with({'state': 'off'})

    def test_тёмная_тема(self):
        with mock.patch.object(d, 'action_set_dark_mode') as act:
            d.find_fast_route('включи тёмную тему')
            act.assert_called_once_with({'enabled': True})

    def test_светлая_тема(self):
        with mock.patch.object(d, 'action_set_dark_mode') as act:
            d.find_fast_route('включи светлую тему')
            act.assert_called_once_with({'enabled': False})

    def test_ночной_режим(self):
        with mock.patch.object(d, 'action_set_night_light') as act:
            d.find_fast_route('включи ночной режим')
            act.assert_called_once_with({'enabled': True})

    def test_заблокировать_экран(self):
        with mock.patch.object(d, 'action_lock_screen') as act:
            d.find_fast_route('заблокируй экран')
            act.assert_called_once_with({})

    def test_спящий_режим(self):
        with mock.patch.object(d, 'action_suspend') as act:
            d.find_fast_route('спящий режим')
            act.assert_called_once_with({})

    # --- поиск и ссылки ---

    def test_покажи_картинку(self):
        with mock.patch.object(d, 'action_open_url') as act:
            result = d.find_fast_route('покажи картинку кота')
            self.assertIn('кота', result)
            act.assert_called_once()
            url = act.call_args[0][0]['url']
            self.assertIn('tbm=isch', url)
            self.assertIn('кот', urllib.parse.unquote(url))

    def test_найди_видео(self):
        with mock.patch.object(d, 'action_open_url') as act:
            d.find_fast_route('найди видео про питона')
            act.assert_called_once()
            url = act.call_args[0][0]['url']
            self.assertIn('youtube.com', url)
            self.assertIn('питон', urllib.parse.unquote(url))

    def test_найди_в_вебе(self):
        with mock.patch.object(d, 'action_open_url') as act:
            d.find_fast_route('поищи новости')
            act.assert_called_once()
            url = act.call_args[0][0]['url']
            self.assertIn('google.com/search', url)
            self.assertIn('новост', urllib.parse.unquote(url))

    def test_открой_сайт(self):
        with mock.patch.object(d, 'action_open_url') as act:
            d.find_fast_route('открой example.com')
            act.assert_called_once_with({'url': 'example.com'})

    def test_открой_url_с_протоколом(self):
        with mock.patch.object(d, 'action_open_url') as act:
            d.find_fast_route('открой https://example.com/путь')
            act.assert_called_once_with({'url': 'https://example.com/путь'})

    # --- протоколы ---

    def test_доброе_утро(self):
        with mock.patch.object(d, 'action_morning_routine') as act:
            d.find_fast_route('доброе утро')
            act.assert_called_once_with({})

    def test_режим_исследования(self):
        with mock.patch.object(d, 'action_learning_routine') as act:
            d.find_fast_route('давай учиться')
            act.assert_called_once_with({})

    def test_начать_работу(self):
        with mock.patch.object(d, 'action_start_work') as act:
            d.find_fast_route('начни работу')
            act.assert_called_once_with({})

    def test_погода(self):
        with mock.patch.object(d, 'action_get_weather') as act:
            d.find_fast_route('какая погода сегодня')
            act.assert_called_once_with({})

    # --- открытие приложений (широкий паттерн — последний) ---

    def test_открой_приложение(self):
        with mock.patch.object(d, 'action_open_app') as act:
            d.find_fast_route('открой браузер')
            act.assert_called_once_with({'name': 'браузер'})

    def test_запусти_терминал(self):
        with mock.patch.object(d, 'action_open_app') as act:
            d.find_fast_route('запусти терминал')
            act.assert_called_once_with({'name': 'терминал'})

    def test_ошибка_в_действии_не_валит_роутер(self):
        with mock.patch.object(d, 'action_open_app',
                               side_effect=RuntimeError('фейл')):
            self.assertIsNone(d.find_fast_route('открой браузер'))


class TestShortcuts(unittest.TestCase):
    """Шорткаты — частые фразы без LLM."""

    def test_время(self):
        self.assertIn('час', d.find_shortcut('который час'))

    def test_дата(self):
        self.assertIn('Сегодня', d.find_shortcut('какое сегодня число'))

    def test_приветствие(self):
        self.assertEqual(d.find_shortcut('привет'),
                         'Привет! Чем могу помочь?')

    def test_неизвестная_фраза(self):
        self.assertIsNone(d.find_shortcut('открой браузер'))

    def test_регистр_не_важен(self):
        self.assertEqual(d.find_shortcut('СПАСИБО'),
                         'Пожалуйста! Обращайтесь.')


class TestComplexRequest(unittest.TestCase):
    def test_сложные_задачи(self):
        for phrase in ('напиши код калькулятора', 'создай сайт',
                       'придумай алгоритм сортировки'):
            self.assertTrue(d.is_complex_request(phrase), phrase)

    def test_простые_команды(self):
        for phrase in ('открой браузер', 'сделай громче',
                       'какой сейчас час'):
            self.assertFalse(d.is_complex_request(phrase), phrase)


class TestAudioHelpers(unittest.TestCase):
    def test_rms_тишины(self):
        self.assertEqual(d.rms(b'\x00' * 1600), 0)

    def test_rms_не_нулевой_для_шума(self):
        self.assertGreater(d.rms((np.ones(160, dtype=np.int16) * 100).tobytes()),
                           0)

    def test_is_silence_с_заданным_фоном(self):
        d._noise_floor = 100.0
        self.assertTrue(d.is_silence(50))     # ниже порога (фон*1.55)
        self.assertFalse(d.is_silence(500))   # выше порога — это речь

    def test_boost_chunk_усиливает_шёпот(self):
        quiet = (np.full(160, 50, dtype=np.int16)).tobytes()
        out = d._boost_chunk(quiet)
        self.assertGreater(d.rms(out), d.rms(quiet))

    def test_boost_chunk_не_искажает_громкий_блок(self):
        loud = (np.full(160, 10000, dtype=np.int16)).tobytes()
        out = d._boost_chunk(loud)
        self.assertLessEqual(d.rms(out), d.rms(loud) * 1.1)


if __name__ == '__main__':
    unittest.main()
