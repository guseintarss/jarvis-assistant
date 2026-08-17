"""Триггеры: проактивные действия по событиям на машине.

Типы триггеров (всё детерминировано, без нейросетей):
    process  — процесс с заданным именем появился в /proc
               (пример: открыт VS Code);
    file     — файл/каталог появился по указанному пути.

Watcher — «по фронту»: срабатывает один раз при появлении условия и
снова после того, как условие исчезло и появилось заново. Состояние
хранится в памяти процесса.
"""

import os

# ============================== ПРОВЕРКИ ====================================


def _processes():
    """Имена процессов из /proc (comm), без дубликатов."""
    names = set()
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/comm', encoding='utf-8',
                          errors='replace') as fh:
                    names.add(fh.read().strip()[:64])
            except OSError:
                continue
    except OSError:
        pass
    return names


def _check_condition(trigger):
    """True, если условие триггера выполнено сейчас."""
    ttype = trigger.get('type', '')
    if ttype == 'process':
        name = trigger.get('name', '')
        return bool(name) and name in _processes()
    if ttype == 'file':
        path = trigger.get('path', '')
        return bool(path) and os.path.exists(path)
    return False


# ============================== WATCHER =====================================


class TriggerWatcher:
    """Проверяет триггеры по фронту и вызывает on_fire(trigger) на срабатывание."""

    def __init__(self, triggers, on_fire, check_fn=None):
        """
        triggers — список dict (process/file);
        on_fire  — callback(trigger) при срабатывании;
        check_fn — функция проверки (по умолчанию _check_condition; тесты
                   подменяют её, чтобы не сканировать настоящий /proc).
        """
        self.triggers = list(triggers or [])
        self.on_fire = on_fire
        self._check = check_fn or _check_condition
        self._active = set()

    def check_once(self):
        """Один проход по триггерам: вызывает on_fire на фронте."""
        for i, trigger in enumerate(self.triggers):
            try:
                ok = bool(self._check(trigger))
            except Exception:  # noqa: BLE001 — сбой проверки не роняет цикл
                ok = False
            if ok and i not in self._active:
                self._active.add(i)
                try:
                    self.on_fire(trigger)
                except Exception:  # noqa: BLE001 — и callback не должен
                    pass           # останавливать планировщик
            elif not ok:
                self._active.discard(i)