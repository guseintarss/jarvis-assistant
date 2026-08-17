"""Уведомления (notify-send) — для проактивных действий планировщика.

notify-send запускается списком аргументов (shell=False), текст не может
стать командой. Параметр notify_fn в планировщике позволяет подменить
отправку (тесты, другой бэкенд).
"""

import subprocess


def notify(title, text):
    """Показывает системное уведомление; True при успехе."""
    try:
        r = subprocess.run(
            ['notify-send', '-a', 'Jarvis', title, text[:2000]],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False