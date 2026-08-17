"""Проактивные действия: напоминания, триггеры, планировщик (ЧАСТЬ 3).

    jarvis.proactive.reminders   — SQLite-хранилище напоминаний (единый
                                   источник правды для инструментов и
                                   планировщика);
    jarvis.proactive.triggers    — триггеры по событиям (process/file),
                                   срабатывание по фронту;
    jarvis.proactive.scheduler   — фоновый планировщик в демоне;
    jarvis.proactive.notify      — уведомления notify-send.

Запускается только в режиме демона (run_daemon), в CLI отсутствует.
"""