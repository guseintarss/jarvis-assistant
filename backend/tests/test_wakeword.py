# -*- coding: utf-8 -*-
"""Тесты wakeword-пакета: признаки, сборка окон, аугментация, модель.

torch-зависимые тесты пропускаются, если torch не установлен
(в рантайме инференса он не нужен — только ONNX).
"""

import unittest
import tempfile
from pathlib import Path

import numpy as np

from wakeword.features import (WIN_SAMPLES, FeatureExtractor, brown_noise,
                               load_wav, log_mel, mel_filterbank, pink_noise,
                               resample_linear, rms, save_wav, scale_to_rms,
                               stft_power, white_noise)
from wakeword.dataset import Augmenter, build_windows, split_stratified


class TestFeatures(unittest.TestCase):
    def test_mel_filterbank_shape_and_bounds(self):
        fb = mel_filterbank()
        self.assertEqual(fb.shape, (40, 257))
        self.assertTrue(np.all(fb >= 0.0))
        # покрытие: полоса 64..7600 Гц = бины 3..243; края шкалы — нули
        covered = fb.sum(axis=0) > 0.0
        self.assertTrue(np.all(covered[3:244]))
        self.assertFalse(np.any(covered[:3]))

    def test_log_mel_shape(self):
        x = np.random.default_rng(0).standard_normal(WIN_SAMPLES).astype(np.float32)
        power = stft_power(x)
        self.assertEqual(power.shape, (257, 97))  # 1 + (16000-512)//160 = 97
        m = log_mel(power, mel_filterbank())
        self.assertEqual(m.shape, (40, 97))
        self.assertTrue(np.all(np.isfinite(m)))

    def test_extractor_and_normalization(self):
        fe = FeatureExtractor()
        rng = np.random.default_rng(1)
        X = np.stack([fe.extract(rng.standard_normal(WIN_SAMPLES).astype(np.float32))
                      for _ in range(10)])
        fe.fit_stats(X)
        self.assertEqual(fe.mean.shape, (40,))
        t = fe.transform(rng.standard_normal(WIN_SAMPLES).astype(np.float32))
        self.assertAlmostEqual(float(t.mean()), 0.0, places=1)
        self.assertAlmostEqual(float(t.std()), 1.0, places=1)

    def test_stats_roundtrip(self):
        fe = FeatureExtractor(mfcc=True, n_mfcc=13)
        fe.mean = np.zeros(13, dtype=np.float32)
        fe.std = np.ones(13, dtype=np.float32)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stats.npz"
            fe.save_stats(str(p))
            fe2 = FeatureExtractor.load_stats(str(p))
        self.assertTrue(fe2.mfcc)
        self.assertEqual(fe2.n_mfcc, 13)

    def test_wav_roundtrip(self):
        x = np.sin(2 * np.pi * 440 * np.arange(1600) / 16000).astype(np.float32) * 0.5
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.wav"
            save_wav(str(p), x)
            y, sr = load_wav(str(p))
        self.assertEqual(sr, 16000)
        self.assertAlmostEqual(float(rms(y - x)), 0.0, places=3)

    def test_resample(self):
        x = np.arange(1000, dtype=np.float32)
        y = resample_linear(x, 16000, 8000)
        self.assertEqual(len(y), 500)
        self.assertAlmostEqual(float(y[250]), 500.0, places=1)

    def test_noise_kinds(self):
        rng = np.random.default_rng(3)
        for kind in (white_noise, pink_noise, brown_noise):
            n = kind(16000, rng)
            self.assertEqual(len(n), 16000)
            self.assertGreater(float(rms(n)), 1e-6)
        s = scale_to_rms(pink_noise(16000, rng), 0.5)
        self.assertAlmostEqual(float(rms(s)), 0.5, places=2)


class TestDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = Path(tempfile.mkdtemp())
        rng = np.random.default_rng(0)
        pos_dir, neg_dir = cls._dir / "pos", cls._dir / "neg"
        pos_dir.mkdir()
        neg_dir.mkdir()
        # «слово»: 0.5 с тона 300 Гц с огибающей (имитация гласной+согласной)
        t = np.arange(int(0.5 * 16000)) / 16000
        word = (np.sin(2 * np.pi * 300 * t) * np.exp(-t * 4)).astype(np.float32)
        for i in range(6):
            save_wav(str(pos_dir / f"p{i}.wav"), word)
        for i in range(6):
            save_wav(str(neg_dir / f"n{i}.wav"),
                     rng.standard_normal(16000).astype(np.float32) * 0.05)
        cls.pos, cls.neg = sorted(pos_dir.glob("*.wav")), sorted(neg_dir.glob("*.wav"))

    def test_build_windows_balance_and_shape(self):
        X, y = build_windows(self.pos, self.neg, windows_per_pos=4,
                             windows_per_neg=4, seed=1)
        self.assertEqual(X.shape[1], WIN_SAMPLES)
        self.assertEqual(X.shape[0], len(y))
        self.assertEqual(int(y.sum()), 24)   # 6 слов * 4 окна
        # негативы: 6 слов * 4 окна + «пол без слова» + «шум без слова»
        # (по половине позитивов = 12 + 12)
        self.assertEqual(int((y == 0).sum()), 24 + 12 + 12)

    def test_augmenter_shapes(self):
        rng = np.random.default_rng(2)
        aug = Augmenter()
        x = rng.standard_normal(WIN_SAMPLES).astype(np.float32)
        for _ in range(10):
            y = aug(x, rng)
            self.assertLessEqual(len(y), WIN_SAMPLES)
            self.assertTrue(np.all(np.isfinite(y)))

    def test_split_stratified(self):
        X = np.arange(100).reshape(100, 1)
        y = np.concatenate([np.ones(60), np.zeros(40)])
        Xtr, ytr, Xva, yva = split_stratified(X, y, val_frac=0.2, seed=0)
        self.assertAlmostEqual(float(ytr.mean()), 0.6, places=2)
        self.assertAlmostEqual(float(yva.mean()), 0.6, places=2)


class TestModel(unittest.TestCase):
    def setUp(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch не установлен")
        self.torch = torch

    def test_forward_and_params(self):
        from wakeword.model import WakeNet
        m = WakeNet()
        x = self.torch.randn(2, 1, 40, 97)
        out = m(x)
        self.assertEqual(out.shape, (2, 1))
        p = m.n_params
        self.assertLess(p, 20000)   # << 500 КБ (54 КБ fp32)
        self.assertLessEqual(p, 14000 + 1)

    def test_export_onnx(self):
        import tempfile
        from wakeword.model import WakeNet, export_onnx
        m = WakeNet()
        with tempfile.TemporaryDirectory() as d:
            path = export_onnx(m, str(Path(d) / "w.onnx"))
            size = Path(path).stat().st_size
        self.assertLess(size, 200_000)


if __name__ == "__main__":
    unittest.main()