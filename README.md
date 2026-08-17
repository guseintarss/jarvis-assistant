# Jarvis — безопасный локальный ассистент для Linux

Модульный бэкенд на Python: локальный классификатор намерений (NumPy MLP),
политика безопасности, исполнитель с инструментами, облачная LLM (бесплатная,
без ключа) для сложных запросов, SQLite FTS5-индекс файлов, bubblewrap-песочница
и D-Bus-сервис, совместимый с расширением GNOME Shell.

Простой пример:

```bash
$ python -m jarvis.main cli "открой браузер"        # (из каталога backend/)
[open_app · 100% · local]
Запускаю: браузер
```

Быстрые однозначные команды исполняются **локально** (классификатор + инструменты,
без LLM); сложные запросы уходят в облачную LLM, которая возвращает только
**JSON-план** — его выполняет локальный исполнитель под контролем политики.

## Что умеет

Локально (мгновенно, без LLM):

- открыть приложение (`открой браузер`), файл (`открой файл отчёт.txt`),
  сайт (`открой сайт github.com`);
- найти файлы по имени (`найди файл отчёт`) и текст в файлах
  (`найди слово TODO в проектах`);
- громкость (`сделай громче на 10 процентов`), яркость (`сделай ярче`),
  управление медиаплеером (`пауза`, `следующий трек`);
- уведомление (`напомни мне выпить воды`), блокировка экрана
  (`заблокируй экран`), скриншот (`сделай скриншот`);
- удаление **в корзину** (`удали файл tmp.txt`) — с подтверждением.

Облачно (бесплатный DeepSeek V4 Flash Free через opencode.ai/zen, без ключа):

- любой диалог: `привет`, `объясни как работает git`, `какой курс доллара`;
- комбинации локальных инструментов, если классификатор не уверен.

Список намерений — `backend/jarvis/intents.py` (15 классов). Слоты извлекаются
регулярными выражениями, а не нейросетью — параметры всегда детерминированы.

## Архитектура

```
Запрос (CLI / D-Bus / расширение GNOME)
   │
   ▼
Классификатор (NumPy MLP, char-граммы, 8192 признаков)
   │  уверенность >= 0.45 ──────────────┐   < 0.45
   ▼                                   ▼
План (локальный намеренный шаг)   Облачная LLM → JSON-план
   │                                   │ (только описание действий,
   ▼                                   ▼  без команд и кода)
Проверка политики: пути, URL, risk, подтверждения
   │
   ▼
Исполнитель → инструменты (pactl, brightnessctl, playerctl,
   fd, rg, xdg-open, gio trash, notify-send, ...)
   │
   ▼
Ответ (текст) → CLI/логи/сигналы D-Bus
```

### Модули (backend/jarvis/)

| Модуль | Назначение |
|---|---|
| `config.py` | пути, пороги, настройки облака (opencode.ai/zen) |
| `intents.py` | реестр 15 намерений + regex-слоты |
| `policy.py`, `policy.yaml` | политика: allowed_roots, denylist, риски, подтверждения |
| `security.py` | PathGuard (realpath-проверки), Confirmator (подтверждения), check_params |
| `ml/` | датасет, char-граммные признаки, MLP (NumPy, Adam, ранняя остановка, температурная калибровка), классификатор |
| `tools/` | 14 инструментов + реестр: desktop (gio/gtk-launch), system (wpctl/brightnessctl/playerctl/notify/lock/скриншот через портал), files (xdg-open/gio trash/URL-валидация), search (fd/rg с deny-globs) |
| `plan.py` | валидация JSON-плана облака (запрещены command/shell/script/exec) |
| `executor.py` | fail-fast исполнение плана, подтверждения, понятные ошибки |
| `cloud/llm.py` | облачный чат: снимок окружения без секретов, системный промпт, JSON-план |
| `indexer/` | SQLite FTS5 (unicode61) по index_roots, переиндексация |
| `sandbox/bwrap.py` | bubblewrap: `--ro-bind / /`, `--tmpfs /tmp`, `--unshare-all`, `--clearenv`, rlimits |
| `dbus/service.py` | org.jarvis.Assistant (совместим с расширением GNOME) |
| `pipeline.py` | сборка: классификатор → план → политика → исполнитель → облако |
| `main.py` | CLI (`cli`), демон (`daemon`), индекс (`index`), обучение (`train`), статус |

### Безопасность

- **Никаких команд от LLM**: облако возвращает только JSON-план действий из
  фиксированного списка инструментов. `command`/`shell`/`script`/`exec`
  запрещены на уровне схемы и валидации.
- **Политика путей**: все пути проходят через `realpath`-проверки PathGuard —
  только внутри `allowed_roots` (по умолчанию `~`), с denylist
  (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config`, `/etc`, ...).
- **Подтверждения**: действия с высоким риском (`move_to_trash`) требуют
  явного согласия в CLI; в демоне (неинтерактивно) — отклоняются с объяснением.
- **Скрипты**: `run_code` исполняется только в bubblewrap-песочнице
  (`--unshare-all`, без сети, read-only корень, tmpfs `/tmp`, rlimits).
- **Удаление** — только `gio trash` (корзина), безвозвратное удаление не
  реализовано.
- **Секреты** не попадают в облако: `environment_snapshot()` отдаёт только
  базовую информацию (ОС, версии утилит, список индексируемых корней) и
  заносит в лог без значений переменных окружения.
- Всё логируется в JSONL (`logger.py`) с редактированием секретов.

## Установка

### Через install.sh (Arch/Debian, GNOME)

```bash
cd jarvis-assistant
chmod +x install.sh
./install.sh
```

Скрипт: ставит системные пакеты, копирует бэкенд в
`~/.local/share/jarvis-assistant/`, создаёт venv (numpy, PyYAML, requests;
PyGObject — симлинк с системного), обучает локальную модель намерений,
устанавливает systemd --user юнит и включает расширение GNOME.

Голосовой режим (слово-активатор, STT, TTS) — опционально:
`./install.sh --with-voice` (Vosk для слова-активатора, faster-whisper
для распознавания, Piper/RHVoice для офлайн-синтеза).

### Вручную

```bash
cd backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -m jarvis.main train     # обучить модель намерений
./venv/bin/python -m jarvis.main index     # построить индекс файлов
./venv/bin/python -m jarvis.main cli       # интерактивная консоль
```

Запуск демона:

```bash
systemctl --user enable --now jarvis-assistant.service
```

(юнит берётся из `systemd/jarvis-assistant.service`; демону нужен доступ к
сессии D-Bus — запускается как user-сервис графической сессии).

## Использование

```bash
# разовый запрос (из каталога установки/backend, где лежит пакет jarvis/)
python -m jarvis.main cli "найди файл отчёт"

# интерактивная консоль (/quit, /index, /train, /status)
python -m jarvis.main cli

# D-Bus демон для расширения GNOME
python -m jarvis.main daemon

# пересобрать индекс файлов / переобучить модель / статус окружения
python -m jarvis.main index && python -m jarvis.main train && python -m jarvis.main status
```

D-Bus интерфейс (`org.jarvis.Assistant`, путь `/org/jarvis/Assistant`):
`Activate`, `Interrupt`, `Stop`, `TogglePause`, `SetActivationMode`,
`ProcessCommand(text) -> response`, свойства `State`/`LastResponse`, сигналы
`StateChanged`/`Heard`/`ResponseReady`/`CommandResult`. Расширение GNOME Shell
использует `ProcessCommand` для текстовых запросов.

## Голосовой режим

Демон слушает слово-активатор «Ева» (Vosk, ~30 МБ модель) и выполняет
команды через тот же пайплайн, что и текст. Возможности:

- слово-активатор с фонетической устойчивостью («йева», «ево» и т.п. не
  активируют, но и не пропускаются огрехи маленькой модели);
- распознавание команд — faster-whisper `small` (int8, CPU), грузится
  лениво при первой команде и выгружается после 10 минут простоя;
- ответ озвучивается: edge-tts (онлайн, «Светлана») → RHVoice (Elena) →
  Piper (irina); короткий «пик» при активации;
- повторное «Ева» во время ответа прерывает озвучку и слушает новую
  команду; диалоговый режим: после ответа слушает уточнения без «Ева»;
- подтверждения опасных действий спрашиваются голосом («Выполнить? Да
  или нет»); без голосового цикла (текстовый D-Bus-запрос) — безопасный
  отказ;
- голос опционален: если нет микрофона/моделей, демон работает текстом
  (`JARVIS_VOICE=0` отключает голос совсем);
- при старте демон возвращает микрофон, если предыдущий экземпляр был
  убит посреди озвучки (защита от «вечного» mute).

Проверка из терминала:

```bash
busctl --user call org.jarvis.Assistant /org/jarvis/Assistant \
  org.jarvis.Assistant ProcessCommand s "открой браузер"
```

## Настройка

- `backend/jarvis/config.py` — пути, `FEATURE_DIM` (8192), порог уверенности
  классификатора (0.45), настройки облака (`CLOUD_BASE_URL`,
  `CLOUD_API_KEY`, `CLOUD_MODEL`), голос (модели Vosk/whisper, голоса TTS,
  `ACTIVATION_MODE` = voice/hotkey/both, диалоговый режим), каталог данных
  (`JARVIS_DATA_DIR`, по умолчанию `~/.local/share/jarvis-assistant`).
- `backend/policy.yaml` — политика: `allowed_roots`, `denied_paths`,
  `index_roots` (7 домашних каталогов), риски инструментов, режимы
  подтверждений (`high=always`), `sandbox`, `cloud` (запрещённые поля).
- Отключить облако можно `JARVIS_CLOUD_ENABLED=0` (сложные запросы ответят
  вежливым отказом), отключить подтверждения — `JARVIS_ASSUME_YES=1`
  (не рекомендуется).

## Тесты

```bash
cd backend
python -m unittest discover -s tests -v
```

75 тестов: политика, безопасность (PathGuard, подтверждения, проверка
параметров), классификатор (обучение, слоты, уверенность), исполнитель
(планы, отказы), слово-активатор (точное/фонетические варианты/ложные
срабатывания), wakeword (признаки, окна, аугментация, модель, ONNX).
Легаси-тесты голосового демона — `tests/legacy_test_daemon.py`
(запускаются отдельно, им нужен старый `jarvis_daemon.py`).

## Wake-word движок «Ева» (экспериментальный)

Собственный крошечный детектор слова-активатора (вместо Vosk) —
14K параметров (~22 КБ ONNX int8), numpy + ONNX на инференсе, torch только
для обучения. Тот же стек: `backend/wakeword/`.

```bash
cd backend
# 1) датасет: TTS (espeak/RHVoice/Piper) «Ева»/«Eva» + confusable («Дева»,
#    «Лева»...) + бытовая речь + шум; плюс запишите свой голос:
python -m wakeword.generate --out data --pos 40 --neg 60
python -m wakeword.record --out data --count 15        # «Ева» своим голосом
python -m wakeword.record --out data --noise 30        # фон комнаты (опц.)

# 2) обучение (нужен torch CPU): BCE + Adam + cosine + early stopping
python -m wakeword.train --data data --out checkpoints --epochs 60

# 3) боевой режим: ring buffer 1.0с, энергетический гейт, порог 0.85,
#    temporal smoothing (2 окна подряд), кулдаун 2.5с
python -m wakeword.infer --model checkpoints/wakeword.onnx --threshold 0.85
python -m wakeword.infer --list          # список микрофонов
python -m wakeword.infer --test-wav ...  # офлайн-прогон по файлу
```

Ограничения честно: на чистой синтетике модель разделяет «Ева»/шум/речь
идеально (AUC 1.0), но confusable-слова («Дева») требуют ваших записей и
большего датасета; 14K параметров — это трейд-офф «размер vs точность».

## Структура проекта

```
jarvis-assistant/
├── backend/
│   ├── jarvis/                  # модульный бэкенд
│   │   ├── main.py              # CLI / демон / index / train / status
│   │   ├── pipeline.py          # сборка пайплайна
│   │   ├── config.py
│   │   ├── policy.py            # + policy.yaml
│   │   ├── security.py          # PathGuard / Confirmator / check_params
│   │   ├── intents.py
│   │   ├── executor.py / plan.py / logger.py
│   │   ├── ml/                  # датасет, признаки, MLP, классификатор
│   │   ├── tools/               # registry + desktop/system/files/search
│   │   ├── cloud/llm.py
│   │   ├── indexer/indexer.py   # SQLite FTS5
│   │   ├── sandbox/bwrap.py     # bubblewrap
│   │   ├── voice/               # голос: wake/audio/stt/tts/engine
│   │   └── dbus/service.py      # org.jarvis.Assistant
│   ├── wakeword/                # крошечный wake-word движок (features/
│   │   │                        # model/dataset/generate/record/train/infer)
│   ├── jarvis_daemon.py         # ЛЕГАСИ: старый голосовой монолит
│   ├── config.py                # ЛЕГАСИ: его конфиг
│   ├── tests/                   # 75 тестов (unittest)
│   └── requirements.txt         # numpy, requests, PyYAML
├── extension/jarvis-assistant@local/   # расширение GNOME Shell (D-Bus клиент)
├── systemd/jarvis-assistant.service
├── install.sh
└── README.md
```

## Ограничения

- Без root/sudo: только user-space инструменты; без `shell=True` везде.
- Удаление — только в корзину; выключение/перезагрузка не реализованы
  (такие запросы вежливо уходят в облако и получают отказ).
- В демоне (неинтерактивно) подтверждения недоступны — опасные действия
  отклоняются с объяснением.
- Облако по умолчанию — бесплатный шлюз opencode.ai/zen: ответы могут быть
  медленными в часы пик; текст запроса уходит в облачный API.
- Индексация ограничена `index_roots` с denylist; поиск идёт по имени файла
  (fd) и содержимому (rg) в рамках политики.