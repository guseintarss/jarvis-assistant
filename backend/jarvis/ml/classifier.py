"""Классификатор намерений: обучение, загрузка, предсказание.

Выход predict():
    {
        'intent':     имя намерения (см. intents.INTENTS),
        'confidence': уверенность 0..1 (вероятность softmax),
        'slots':      параметры, извлечённые регэкспами (intents.py),
        'risk':       уровень риска намерения из политики,
    }
"""

import hashlib
import os

import numpy as np

from jarvis import intents
from jarvis import logger
from jarvis.ml import dataset
from jarvis.ml import features
from jarvis.ml.model import MLP


def _dataset_hash():
    """Хэш датасета — чтобы переобучать модель при изменении фраз."""
    texts, labels = dataset.load_dataset()
    payload = '\n'.join(f'{t}|{l}' for t, l in zip(texts, labels))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class IntentClassifier:
    """Локальная модель классификации намерений (NumPy MLP + char-граммы)."""

    def __init__(self, model_path, feature_dim=4096, confidence_threshold=0.45):
        self.model_path = model_path
        self.feature_dim = feature_dim
        self.confidence_threshold = confidence_threshold
        self.classes = list(intents.CLASS_NAMES)
        self.class_index = {name: i for i, name in enumerate(self.classes)}
        self.mlp = None
        self.dataset_hash = _dataset_hash()

    # --------------------------- обучение ---------------------------------

    def train(self, epochs=400, val_fraction=0.15):
        """Обучает с нуля на встроенном датасете, сохраняет npz.

        Возвращает (val_accuracy, num_samples).
        """
        texts, labels = dataset.load_dataset()
        x = features.vectorize_many(texts, self.feature_dim)
        y = np.array([self.class_index[l] for l in labels], dtype=np.int64)

        # детерминированное разбиение train/val
        rng = np.random.default_rng(seed=42)
        idx = rng.permutation(len(y))
        n_val = max(1, int(len(y) * val_fraction))
        val_idx, train_idx = idx[:n_val], idx[n_val:]
        self.mlp = MLP(self.feature_dim, num_classes=len(self.classes))
        val_acc, history = self.mlp.fit(
            x[train_idx], y[train_idx], x[val_idx], y[val_idx],
            epochs=epochs)
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        self.mlp.save(self.model_path)
        self._save_hash()
        logger.get_logger().event(
            'model_trained', path=self.model_path,
            samples=len(y), classes=len(self.classes),
            val_accuracy=round(val_acc, 4), dataset_hash=self.dataset_hash)
        return val_acc, len(y)

    # --------------------------- загрузка ---------------------------------

    def ensure_trained(self):
        """Загружает модель; если файла нет или датасет изменился — обучает."""
        if self.mlp is not None:
            return
        if (os.path.isfile(self.model_path)
                and self._saved_hash() == self.dataset_hash):
            self.mlp = MLP.load(self.model_path)
        else:
            self.train()

    def _saved_hash(self):
        try:
            with open(self.model_path + '.hash', encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            return ''

    def _save_hash(self):
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        with open(self.model_path + '.hash', 'w', encoding='utf-8') as f:
            f.write(self.dataset_hash)

    # --------------------------- предсказание ------------------------------

    def predict(self, text):
        """Текст -> {intent, confidence, slots, risk}."""
        self.ensure_trained()
        text = (text or '').strip()
        if not text:
            return {'intent': 'chat', 'confidence': 0.0,
                    'slots': {}, 'risk': intents.RISK_LOW}
        x = features.vectorize(text, self.feature_dim).reshape(1, -1)
        proba = self.mlp.predict_proba(x)[0]
        idx = int(np.argmax(proba))
        confidence = float(proba[idx])
        intent_name = self.classes[idx]
        return {
            'intent': intent_name,
            'confidence': round(confidence, 4),
            'slots': intents.extract_slots(intent_name, text),
            'risk': intents.INTENTS[intent_name].risk,
        }

    def predict_many(self, texts):
        return [self.predict(t) for t in texts]