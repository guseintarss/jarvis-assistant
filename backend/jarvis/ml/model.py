"""Простая нейросеть на чистом NumPy — многослойный перцептрон.

Архитектура (намеренно маленькая — обучается за секунды на CPU):

    DIM (4096) -> 128 (ReLU) -> 64 (ReLU) -> C классов (softmax)

Оптимизатор Adam с ранней остановкой по точности на валидационной
выборке. Всё детерминировано: фиксированный seed, чтобы обучение
было воспроизводимым (и тесты не «плавали»).
"""

import numpy as np

SEED = 42


def softmax(z):
    z = z - np.max(z, axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=1, keepdims=True)


def relu(x):
    """LeakyReLU (slope 0.1): мёртвые нейроны ReLU приводят к застреванию
    обучения (градиенты -> 0); leaky-вариант этого избегает."""
    return np.maximum(x, 0.1 * x)


def relu_grad(x):
    """Градиент LeakyReLU: 1.0 для x>0, иначе 0.1."""
    return np.where(x > 0, 1.0, 0.1)


class MLP:
    """Двухскрытый перцептрон с Adam и ранней остановкой."""

    def __init__(self, input_dim, hidden_sizes=(256, 128), num_classes=15,
                 seed=SEED):
        rng = np.random.default_rng(seed)
        self.dims = [input_dim, *hidden_sizes, num_classes]
        self.params = []
        for i in range(len(self.dims) - 1):
            # He-инициализация для ReLU
            scale = np.sqrt(2.0 / self.dims[i])
            w = rng.normal(0, scale, (self.dims[i], self.dims[i + 1]))
            b = np.zeros(self.dims[i + 1])
            self.params.append((w.astype(np.float32), b.astype(np.float32)))
        self.temperature = 1.0

    def forward(self, x):
        """Прямой проход: список активаций слоёв."""
        acts = [x]
        a = x
        for i, (w, b) in enumerate(self.params):
            a = relu(a @ w + b) if i < len(self.params) - 1 \
                else a @ w + b
            acts.append(a)
        return acts

    def predict_proba(self, x):
        """Вероятности классов для матрицы признаков.

        Логиты делятся на калибровочную температуру: на маленьком датасете
        margin после обучения оказывается ~1.0 nat, и «сырой» softmax даёт
        плоские вероятности (~0.13 при 15 классах). Температура подбирается
        по валидационной выборке в конце fit().
        """
        logits = self.forward(x)[-1]
        return softmax(logits / self.temperature)

    def predict(self, x):
        return np.argmax(self.predict_proba(x), axis=1)

    # -------------------------- обучение ---------------------------------

    def fit(self, x_train, y_train, x_val, y_val,
            epochs=300, batch_size=16, lr=1e-3, patience=25,
            weight_decay=1e-4):
        """Обучает с Adam; возвращает (best_val_acc, history)."""
        n = len(y_train)
        num_classes = self.dims[-1]
        rng = np.random.default_rng(seed=SEED)
        m_w = [np.zeros_like(w) for w, _ in self.params]
        m_b = [np.zeros_like(b) for _, b in self.params]
        v_w = [np.zeros_like(w) for w, _ in self.params]
        v_b = [np.zeros_like(b) for _, b in self.params]
        t = 0

        best_acc, best_params, best_epoch = 0.0, None, 0
        self._best_key = (0.0, float('inf'))
        history = []

        def one_hot(idx):
            y = np.zeros((len(idx), num_classes), dtype=np.float32)
            y[np.arange(len(idx)), idx] = 1.0
            return y

        for epoch in range(epochs):
            perm = rng.permutation(n)
            x_s, y_s = x_train[perm], y_train[perm]
            for start in range(0, n, batch_size):
                xb = x_s[start:start + batch_size]
                yb = one_hot(y_s[start:start + batch_size])
                acts = self.forward(xb)
                # градиент softmax-кросс-энтропии
                dz = acts[-1] - yb
                grads_w, grads_b = [], []
                for i in range(len(self.params) - 1, -1, -1):
                    a_prev = acts[i]
                    dw = a_prev.T @ dz / batch_size
                    db = dz.mean(axis=0)
                    grads_w.append(dw)
                    grads_b.append(db)
                    if i > 0:
                        dz = (dz @ self.params[i][0].T) * relu_grad(acts[i])
                grads_w.reverse()
                grads_b.reverse()

                t += 1
                beta1, beta2, eps = 0.9, 0.999, 1e-8
                for i in range(len(self.params)):
                    # L2-регуляризация: штраф за большие веса
                    grads_w[i] = grads_w[i] + weight_decay * self.params[i][0]
                    m_w[i] = beta1 * m_w[i] + (1 - beta1) * grads_w[i]
                    m_b[i] = beta1 * m_b[i] + (1 - beta1) * grads_b[i]
                    v_w[i] = beta2 * v_w[i] + (1 - beta2) * grads_w[i] ** 2
                    v_b[i] = beta2 * v_b[i] + (1 - beta2) * grads_b[i] ** 2
                    mw_hat = m_w[i] / (1 - beta1 ** t)
                    mb_hat = m_b[i] / (1 - beta1 ** t)
                    vw_hat = v_w[i] / (1 - beta2 ** t)
                    vb_hat = v_b[i] / (1 - beta2 ** t)
                    w, b = self.params[i]
                    self.params[i] = (w - lr * mw_hat / (np.sqrt(vw_hat) + eps),
                                      b - lr * mb_hat / (np.sqrt(vb_hat) + eps))

            if x_val is not None and len(x_val):
                pred = self.predict(x_val)
                acc = float(np.mean(pred == y_val))
                # CE на валидации — метрика КАЛИБРОВКИ: при равной точности
                # выбираем чекпойнт с более «уверенными» вероятностями
                # (меньшим cross-entropy), иначе softmax остаётся плоским.
                proba = self.predict_proba(x_val)
                ce_val = float(-np.log(
                    proba[np.arange(len(y_val)), y_val] + 1e-9).mean())
                key = (acc, -ce_val)
                if key > self._best_key:
                    self._best_key = key
                    best_acc = acc
                    best_params = [(w.copy(), b.copy()) for w, b in self.params]
                    best_epoch = epoch
                history.append(acc)
                # ранняя остановка
                if epoch - best_epoch > patience:
                    break

        if best_params is not None:
            self.params = best_params
        # калибровка температуры: подбираем T так, чтобы softmax(logits/T)
        # давал «честные» вероятности на валидации (T < 1 -> острее)
        if x_val is not None and len(x_val):
            self._calibrate(x_val, y_val)
        return best_acc, history

    def _calibrate(self, x_val, y_val):
        """Подбор температуры по NLL на валидации (перебор по сетке)."""
        logits = self.forward(x_val)[-1]
        yv = y_val.astype(np.int64)
        best_t, best_nll = 1.0, None
        for t in np.logspace(np.log10(0.02), 0.0, 60):
            p = softmax(logits / t)
            nll = float(-np.log(
                p[np.arange(len(yv)), yv] + 1e-12).mean())
            if best_nll is None or nll < best_nll:
                best_t, best_nll = t, nll
        self.temperature = float(best_t)

    # -------------------------- сериализация ------------------------------

    def save(self, path):
        data = {'dims': np.array(self.dims),
                'temperature': np.array(self.temperature)}
        for i, (w, b) in enumerate(self.params):
            data[f'w{i}'] = w
            data[f'b{i}'] = b
        np.savez_compressed(path, **data)

    @classmethod
    def load(cls, path):
        data = np.load(path, allow_pickle=False)
        dims = [int(d) for d in data['dims']]
        mlp = cls(dims[0], dims[1:-1], dims[-1])
        mlp.params = [(data[f'w{i}'], data[f'b{i}'])
                      for i in range(len(dims) - 1)]
        if 'temperature' in data:
            mlp.temperature = float(data['temperature'])
        return mlp