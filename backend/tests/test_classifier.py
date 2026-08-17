"""Тесты локального классификатора намерений.

Проверяем: обучение сходится, знакомые фразы распознаются, извлечение
слотов корректно, выход содержит intent/confidence/slots/risk.

Запуск (из каталога backend/):
    python -m unittest tests.test_classifier -v
"""

import os
import tempfile
import unittest

from jarvis.ml import dataset
from jarvis.ml.classifier import IntentClassifier
from jarvis.ml.features import vectorize, vectorize_many
from jarvis.ml.model import MLP


def _make_classifier(tmp):
    return IntentClassifier(
        os.path.join(tmp, 'model.npz'), feature_dim=8192,
        confidence_threshold=0.45)


class TestFeatures(unittest.TestCase):

    def test_vector_shape_and_norm(self):
        v = vectorize('открой браузер', dim=2048)
        self.assertEqual(v.shape, (2048,))
        # L2-норма == 1
        self.assertAlmostEqual(float(v.sum() ** 2) and
                               float((v ** 2).sum()) ** 0.5, 1.0, places=4)

    def test_deterministic(self):
        self.assertTrue((vectorize('привет', 512) ==
                         vectorize('привет', 512)).all())

    def test_matrix(self):
        x = vectorize_many(['а', 'б'], 512)
        self.assertEqual(x.shape, (2, 512))


class TestMLP(unittest.TestCase):

    def test_predict_shape_and_proba(self):
        mlp = MLP(64, (16,), 3)
        x = __import__('numpy').random.default_rng(0).normal(
            size=(5, 64)).astype('float32')
        proba = mlp.predict_proba(x)
        self.assertEqual(proba.shape, (5, 3))
        self.assertTrue(((proba >= 0) & (proba <= 1)).all())
        # вероятности по строкам суммируются в 1
        sums = proba.sum(axis=1)
        self.assertTrue((abs(sums - 1) < 1e-4).all())


class TestClassifier(unittest.TestCase):

    def test_training_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            clf = _make_classifier(tmp)
            val_acc, samples = clf.train(epochs=250)
            self.assertGreater(samples, 50)
            # на простом датасете модель обязана сойтись хорошо
            self.assertGreater(val_acc, 0.5)

    def test_predict_output_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            clf = _make_classifier(tmp)
            clf.train(epochs=60)
            out = clf.predict('открой браузер')
            self.assertIn('intent', out)
            self.assertIn('slots', out)
            self.assertIn('confidence', out)
            self.assertIn('risk', out)
            self.assertGreaterEqual(out['confidence'], 0.0)
            self.assertLessEqual(out['confidence'], 1.0)
            self.assertIn(out['risk'], ('low', 'medium', 'high'))

    def test_recognizes_training_phrases(self):
        """Фразы из датасета должны распознаваться (слота извлекаются)."""
        with tempfile.TemporaryDirectory() as tmp:
            clf = _make_classifier(tmp)
            clf.train(epochs=120)
            cases = [
                ('открой браузер', 'open_app'),
                ('сделай громче', 'volume_up'),
                ('сделай тише', 'volume_down'),
                ('сделай скриншот', 'screenshot'),
                ('заблокируй экран', 'lock_screen'),
                ('найди файл отчёт', 'search_files'),
                ('сделай ярче', 'brightness_up'),
                ('привет', 'chat'),
            ]
            hits = sum(1 for text, want in cases
                       if clf.predict(text)['intent'] == want)
            self.assertGreaterEqual(hits, 6)

    def test_slots_extraction(self):
        from jarvis import intents as intents_mod
        self.assertEqual(intents_mod.extract_slots('open_app', 'открой браузер'),
                         {'app': 'браузер'})
        self.assertEqual(intents_mod.extract_slots('open_app', 'запусти spotify'),
                         {'app': 'spotify'})
        self.assertEqual(intents_mod.extract_slots('open_url', 'открой сайт github.com'),
                         {'url': 'github.com'})
        self.assertEqual(intents_mod.extract_slots('volume_up', 'прибавь на 10 процентов'),
                         {'step': '10'})
        self.assertEqual(intents_mod.extract_slots('move_to_trash', 'удали файл tmp.txt'),
                         {'path': 'tmp.txt'})

    def test_empty_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            clf = _make_classifier(tmp)
            clf.train(epochs=30)
            out = clf.predict('')
            self.assertEqual(out['intent'], 'chat')
            self.assertEqual(out['confidence'], 0.0)


if __name__ == '__main__':
    unittest.main()