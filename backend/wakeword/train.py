# -*- coding: utf-8 -*-
"""ШАГ 4. Обучение: BCEWithLogitsLoss + Adam + early stopping.

Метрики считаются вручную на numpy (без sklearn):
  - AUC: Mann-Whitney U / (n_pos*n_neg) — вероятность, что случайный
    позитив получит больший скор, чем случайный негатив;
  - порог: выбирается по валидации двумя способами:
      * best-F1: баланс precision/recall (если не указать иначе);
      * thr@FPR<0.02: порог, при котором доля ложных срабатываний < 2%
        (0.02*24 окна/мин*60 мин ≈ 29 ложных «Ева» в час — приемлемо
        для старта; с реальными данными порог подкручивается).
  - Learning rate: cosine от lr до lr/100 за все эпохи — плавное
    «вхождение» в минимум в конце, без ручного подбора моментов сброса.
  - Early stopping: следим за val loss, ждём patience эпох без улучшения.

Сохранение: checkpoints/best.pt (веса+статистики+порог) и экспорт ONNX
int8 (wakeword.onnx) для этапа 5.

Использование:
    python -m wakeword.train --data data --out checkpoints \
        --epochs 60 --batch 64 --patience 10
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

from .dataset import (WakeDataset, build_windows, split_stratified)
from .features import FeatureExtractor
from .model import WakeNet, export_onnx


# ---------------------------------------------------------------------------
# Метрики без sklearn
# ---------------------------------------------------------------------------

def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Площадь под ROC. Ранги: U = sum(rank_pos) - n_pos(n_pos+1)/2."""
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(y) + 1)
    u = ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


def threshold_metrics(y: np.ndarray, p: np.ndarray, thr: float
                      ) -> dict[str, float]:
    pred = p >= thr
    tp = float((pred & (y == 1)).sum())
    fp = float((pred & (y == 0)).sum())
    fn = float((~pred & (y == 1)).sum())
    tn = float((~pred & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec,
            "recall": rec, "f1": f1, "fpr": fpr}


def best_thresholds(y: np.ndarray, p: np.ndarray
                    ) -> tuple[float, float]:
    """(thr_best_f1, thr@fpr<0.02) по валидации."""
    thr_f1, best = 0.5, -1.0
    thr_fpr = 1.0
    for thr in np.linspace(0.05, 0.98, 94):
        m = threshold_metrics(y, p, float(thr))
        if m["f1"] > best:
            best, thr_f1 = m["f1"], thr
        if m["fpr"] <= 0.02:
            thr_fpr = min(thr_fpr, thr)
    if thr_fpr == 1.0:  # FPR<2% недостижим — берём 0.9 как «строгий»
        thr_fpr = 0.9
    return thr_f1, thr_fpr


# ---------------------------------------------------------------------------
# Обучение
# ---------------------------------------------------------------------------

def train_epoch(model, loader, opt, sched, device, pos_weight) -> float:
    model.train()
    total, n = 0.0, 0
    crit = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    for xb, yb in loader:
        xb, yb = xb.to(device), yb.to(device)
        opt.zero_grad()
        loss = crit(model(xb).squeeze(1), yb)
        loss.backward()
        opt.step()
        total += loss.item() * len(yb)
        n += len(yb)
    sched.step()
    return total / n


@torch.no_grad()
def evaluate(model, loader, device) -> tuple[float, np.ndarray, np.ndarray]:
    model.eval()
    ys, ps = [], []
    for xb, yb in loader:
        p = torch.sigmoid(model(xb.to(device))).squeeze(1).cpu().numpy()
        ys.append(yb.numpy())
        ps.append(p)
    y = np.concatenate(ys)
    p = np.concatenate(ps)
    loss = nn.functional.binary_cross_entropy(
        torch.tensor(p), torch.tensor(y)).item()
    return loss, y, p


def main() -> None:
    ap = argparse.ArgumentParser(description="Обучение WakeNet")
    ap.add_argument("--data", default="data", help="каталог wav (pos/, neg/, recorded/)")
    ap.add_argument("--out", default="checkpoints")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--win-per-sample", type=int, default=8,
                    help="окон на каждый wav")
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    device = torch.device(args.device)

    data_dir = Path(args.data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pos_files = sorted((data_dir / "pos").glob("*.wav")) + \
        sorted((data_dir / "recorded" / "pos").glob("*.wav"))
    neg_files = sorted((data_dir / "neg").glob("*.wav")) + \
        sorted((data_dir / "recorded" / "neg").glob("*.wav")) + \
        sorted((data_dir / "recorded" / "noise").glob("*.wav"))
    room = next((data_dir / "recorded" / "noise").glob("*.wav"), None)
    if not pos_files or not neg_files:
        raise SystemExit(
            "Нет wav-файлов. Сначала: python -m wakeword.generate --out data")

    print(f"==> Окна: {len(pos_files)} позитивных wav, {len(neg_files)} негативных")
    X, y = build_windows(pos_files, neg_files, room_noise=room,
                         windows_per_pos=args.win_per_sample,
                         windows_per_neg=args.win_per_sample, seed=args.seed)
    print(f"    окон: {len(X)} (позитивов {int(y.sum())}, "
          f"негативов {int((y == 0).sum())})")

    Xtr, ytr, Xva, yva = split_stratified(X, y, val_frac=0.2, seed=args.seed)

    # Статистики нормализации — ТОЛЬКО по train (иначе утечка)
    fe = FeatureExtractor()
    fe.fit_stats(np.stack([fe.extract(x) for x in Xtr]))
    fe.save_stats(str(out_dir / "stats.npz"))
    print(f"    нормализация: mean/std по {fe.n_mels} мел-бинам (train)")

    train_ds = WakeDataset(Xtr, ytr, fe, augment=True, seed=args.seed)
    val_ds = WakeDataset(Xva, yva, fe, augment=False, seed=args.seed)
    train_loader = torch.utils.data.DataLoader(
        train_ds, batch_size=args.batch, shuffle=True, num_workers=0)
    val_loader = torch.utils.data.DataLoader(
        val_ds, batch_size=args.batch, shuffle=False, num_workers=0)

    model = WakeNet(n_mels=fe.n_mels).to(device)
    print(f"==> Модель: {model.n_params} параметров "
          f"({model.n_params * 4 / 1024:.1f} КБ fp32)")

    # Дисбаланс классов (негативов обычно в разы больше): BCE с pos_weight
    # штрафует пропуски позитивов сильнее (pos_weight = n_neg/n_pos).
    # Кап 4.0: слишком большой вес делает модель «палящей всё подряд»
    # (лучше перебить парой негативов, чем ловить каждый шум).
    n_pos = float(ytr.sum())
    n_neg = float(len(ytr) - n_pos)
    pos_weight = torch.tensor(min(4.0, max(1.0, n_neg / n_pos)), device=device)
    print(f"    pos_weight={pos_weight.item():.2f} (кап 4.0)")

    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)

    best_loss, best_auc, patience, t0 = float("inf"), -1.0, 0, time.time()
    for ep in range(1, args.epochs + 1):
        tr_loss = train_epoch(model, train_loader, opt, sched, device,
                              pos_weight)
        val_loss, yv, pv = evaluate(model, val_loader, device)
        a = auc(yv, pv)
        m = threshold_metrics(yv, pv, 0.5)
        print(f"ep {ep:3d}/{args.epochs} | train {tr_loss:.4f} "
              f"val {val_loss:.4f} | AUC {a:.3f} | acc@0.5 "
              f"{(m['tp'] + m['tn']) / len(yv):.3f} | "
              f"F1 {m['f1']:.3f} ({m['tp']:.0f}/{m['fp']:.0f}/{m['fn']:.0f}) "
              f"| lr {sched.get_last_lr()[0]:.1e}")

        if val_loss < best_loss - 1e-4:
            best_loss, patience = val_loss, 0
            best_auc = a
            thr_f1, thr_fpr = best_thresholds(yv, pv)
            torch.save({
                "state_dict": model.state_dict(),
                "stats": {"mean": fe.mean, "std": fe.std},
                "n_mels": fe.n_mels, "config": vars(args),
                "val_auc": a, "thr_f1": thr_f1, "thr_fpr": thr_fpr,
            }, out_dir / "best.pt")
        else:
            patience += 1
            if patience >= args.patience:
                print(f"    early stop на эпохе {ep}")
                break

    ck = torch.load(out_dir / "best.pt", map_location="cpu",
                    weights_only=False)
    print(f"==> Лучшая модель (AUC {ck['val_auc']:.3f}, "
          f"{time.time() - t0:.0f} с)")
    print(f"    порог best-F1: {ck['thr_f1']:.2f} | "
          f"порог FPR<2%: {ck['thr_fpr']:.2f}")
    model.load_state_dict(ck["state_dict"])
    onnx_path = export_onnx(model, str(out_dir / "wakeword.onnx"),
                            n_mels=ck["n_mels"])
    size = Path(onnx_path).stat().st_size / 1024
    print(f"    ONNX int8: {onnx_path} ({size:.1f} КБ)")
    print(f"    Для infer: python -m wakeword.infer --model {onnx_path} "
          f"--threshold {ck['thr_f1']:.2f}")


if __name__ == "__main__":
    main()