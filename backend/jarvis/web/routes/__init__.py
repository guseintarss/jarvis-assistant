"""Роуты веб-панели Jarvis (ЧАСТЬ 6).

    chat      — REST /api/chat + WebSocket /ws/chat + история;
    reminders — CRUD напоминаний (единый ReminderStore с планировщиком);
    settings  — просмотр/изменение policy.yaml (включение, риск) + статус;
    logs      — хвост JSONL-лога.
"""