"""Тесты распознавания слова-активатора (jarvis.voice.wake)."""

import unittest

from jarvis.voice import wake


class WakeWordTest(unittest.TestCase):

    def test_exact(self):
        self.assertTrue(wake.contains_wake_word('ева'))
        self.assertTrue(wake.contains_wake_word('эва'))
        self.assertTrue(wake.contains_wake_word('Ева, открой браузер'))
        self.assertTrue(wake.contains_wake_word('слушай ева сделай громче'))

    def test_distorted(self):
        # Варианты распознавания маленькой модели
        self.assertTrue(wake.contains_wake_word('ево'))
        self.assertTrue(wake.contains_wake_word('эво'))
        self.assertTrue(wake.contains_wake_word('еву'))
        self.assertTrue(wake.contains_wake_word('йева'))
        self.assertTrue(wake.contains_wake_word('ъева'))
        self.assertTrue(wake.contains_wake_word('еваа'))

    def test_false_friends(self):
        # «ева» в составе чужих слов — активации не должно быть
        for word in ('дева', 'нева', 'лева', 'дива', 'лива', 'слива',
                     'нива', 'тива', 'тиво', 'еве', 'ёва', 'йова', 'вева',
                     'жива', 'живе', 'рева', 'евро', 'евра', 'евре', 'евва'):
            self.assertFalse(wake.contains_wake_word(word),
                             f'«{word}» не должен активировать')

    def test_random_words(self):
        for word in ('ава', 'тва', 'ява', 'браузер', 'привет', 'время',
                     'который час', 'открой файлы'):
            self.assertFalse(wake.contains_wake_word(word),
                             f'«{word}» не должен активировать')

    def test_strip(self):
        self.assertEqual(wake.strip_wake_word('ева, открой браузер'),
                         'открой браузер')
        self.assertEqual(wake.strip_wake_word('ево открой'), 'открой')
        self.assertEqual(wake.strip_wake_word('йева открой'), 'открой')
        # чужие слова не трогаем
        self.assertEqual(wake.strip_wake_word('дева открой'), 'дева открой')
        self.assertEqual(wake.strip_wake_word('лева'), 'лева')
        self.assertEqual(wake.strip_wake_word('йова открой'), 'йова открой')
        # нет слова — текст без изменений
        self.assertEqual(wake.strip_wake_word('открой браузер'), 'открой браузер')
        self.assertEqual(wake.strip_wake_word(''), '')

    def test_empty(self):
        self.assertFalse(wake.contains_wake_word(''))
        self.assertFalse(wake.contains_wake_word(None))


if __name__ == '__main__':
    unittest.main()