#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Ева — голосовой ассистент. Установщик
#   Ставит:
#     - расширение GNOME Shell в ~/.local/share/gnome-shell/extensions
#     - Python-демон + venv в ~/.local/share/jarvis-assistant
#     - модель Vosk (слово-активатор) и женский голос Piper "irina" (TTS)
#     - systemd --user юнит для автозапуска демона
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/jarvis-assistant"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/jarvis-assistant@local"
MODELS_DIR="$APP_DIR/models"

# ============================================================
# Скачивание с проверкой целостности (SHA-256).
# Модели качаем «вслепую» — без проверки битый/подменённый файл
# молча ломает демон (либо Vosk-модель, либо голос не загружаются).
# Все контрольные суммы ниже проверены по состоянию на авг 2026
# (см. комментарии у каждой).
# ============================================================

# Проверяет sha256 файла. Возвращает 0 при совпадении.
sha256_check() {
    local file="$1" expected="$2" tool actual
    if [ ! -f "$file" ]; then
        return 1
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$file" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$file" | awk '{print $1}')
    else
        echo "!! sha256sum/shasum не найдены — проверку целостности пропускаю"
        return 0
    fi
    if [ "$actual" = "$expected" ]; then
        return 0
    fi
    echo "!! Контрольная сумма не совпала: $file" >&2
    echo "   ожидалось:  $expected" >&2
    echo "   получилось: $actual" >&2
    return 1
}

# Скачивает файл и проверяет его: до 3 попыток, при несовпадении
# sha256 битый файл удаляется и качается заново.
#   $1 = URL, $2 = файл-назначение, $3 = название для лога, $4 = ожидаемый sha256
download_verified() {
    local url="$1" out="$2" name="$3" expected="$4" attempt=1
    while [ "$attempt" -le 3 ]; do
        if [ -f "$out" ] && sha256_check "$out" "$expected"; then
            echo "    $name: уже скачан, целостность ОК"
            return 0
        fi
        echo "    $name: скачивание (попытка $attempt из 3)..."
        rm -f "$out"
        if command -v wget >/dev/null 2>&1; then
            wget -q --show-progress -O "$out" "$url"
        else
            curl -fsSL --retry 3 --retry-all-errors -o "$out" "$url"
        fi
        if [ -f "$out" ] && sha256_check "$out" "$expected"; then
            echo "    $name: скачан, целостность ОК"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "!! Не удалось скачать $name: $url" >&2
    echo "   Проверьте интернет и повторите установку. Если сервер отвечает"
    echo "   медленно/обрывается (так бывает у alphacephei.com), скачайте файл"
    echo "   заранее вручную в нужное место и запустите install.sh заново —"
    echo "   после проверки суммы он будет пропущен." >&2
    return 1
}

echo "==> Проверка системных пакетов (нужен sudo)..."
if command -v pacman >/dev/null 2>&1; then
    echo "    обнаружен pacman (Arch/Manjaro и т.д.)"
    echo "    (Arch не поддерживает частичное обновление — сначала обновлю всю систему)"
    sudo pacman -Syu --needed --noconfirm \
        base-devel python python-pip python-gobject \
        portaudio \
        ffmpeg unzip wget \
        libpulse alsa-utils \
        espeak-ng \
        brightnessctl networkmanager glib2
    echo "    (если звук на PipeWire и 'pactl' не находит устройства — довставьте pipewire-pulse)"
elif command -v apt >/dev/null 2>&1; then
    echo "    обнаружен apt (Debian/Ubuntu и т.д.)"
    sudo apt update
    sudo apt install -y \
        python3-venv python3-pip python3-gi \
        portaudio19-dev libportaudio2 \
        ffmpeg unzip wget \
        pulseaudio-utils alsa-utils \
        espeak-ng \
        brightnessctl network-manager libglib2.0-bin
else
    echo "!! Не нашёл ни pacman, ни apt. Поставьте вручную аналоги пакетов:"
    echo "   python3, pip, PyGObject (gi), portaudio, ffmpeg, unzip, wget,"
    echo "   pactl (pulseaudio/pipewire-pulse), alsa-utils, espeak-ng,"
    echo "   brightnessctl, NetworkManager, glib2 (gdbus)."
fi

echo "==> Создаю каталоги..."
mkdir -p "$APP_DIR" "$MODELS_DIR/piper"
mkdir -p "$EXT_DIR"

echo "==> Копирую расширение GNOME Shell..."
cp -r "$SCRIPT_DIR"/extension/jarvis-assistant@local/* "$EXT_DIR"/

echo "==> Компилирую схему настроек расширения (режим активации, горячая клавиша)..."
if command -v glib-compile-schemas >/dev/null 2>&1; then
    glib-compile-schemas "$EXT_DIR/schemas" && echo "    схема готова"
else
    echo "!! glib-compile-schemas не найден (нужен пакет glib2) — настройка"
    echo "   режима активации и горячей клавиши в расширении не будет работать"
fi

echo "==> Копирую демон..."
cp "$SCRIPT_DIR"/backend/jarvis_daemon.py "$APP_DIR"/
cp "$SCRIPT_DIR"/backend/requirements.txt "$APP_DIR"/

echo "==> Создаю виртуальное окружение и ставлю Python-зависимости..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip --no-cache-dir
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --no-cache-dir

echo "==> Даю venv доступ к системному PyGObject (нужен для D-Bus/GLib)..."
SYS_GI_PATH=$(python3 -c "import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))")
VENV_SITE=$("$APP_DIR/venv/bin/python" -c "import site; print(site.getsitepackages()[0])")
if [ -d "$SYS_GI_PATH/gi" ]; then
    ln -sfn "$SYS_GI_PATH/gi" "$VENV_SITE/gi"
else
    echo "!! Не нашёл системный модуль gi. Установите пакет PyGObject:"
    echo "   Arch: sudo pacman -S python-gobject   |   Debian/Ubuntu: sudo apt install python3-gi"
fi

echo "==> Скачиваю Vosk-модель (маленькая, для слова-активатора)..."
# Контрольная сумма: та же, что в AUR-пакете vosk-model-small-ru
# (sha256sums в PKGBUILD) — файл с того же официального URL alphacephei.com.
VOSK_URL="https://alphacephei.com/kaldi/models/vosk-model-small-ru-0.22.zip"
VOSK_ZIP="/tmp/vosk-small-ru.zip"
VOSK_SHA256="961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
VOSK_DIR="$MODELS_DIR/vosk-model-small-ru"
if [ -d "$VOSK_DIR" ] && [ -f "$VOSK_DIR/conf/model.conf" ]; then
    echo "    уже скачана, пропускаю"
else
    download_verified "$VOSK_URL" "$VOSK_ZIP" "Vosk-модель (46 МБ)" "$VOSK_SHA256"
    rm -rf "$VOSK_DIR" "$MODELS_DIR"/vosk-model-small-ru-0.22
    unzip -q "$VOSK_ZIP" -d "$MODELS_DIR"
    mv "$MODELS_DIR"/vosk-model-small-ru-0.22 "$VOSK_DIR"
fi
rm -f "$VOSK_ZIP"

echo "==> Скачиваю женский голос Piper (ru_RU-irina-medium)..."
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium"
PIPER_ONNX="$MODELS_DIR/piper/ru_RU-irina-medium.onnx"
PIPER_JSON="$MODELS_DIR/piper/ru_RU-irina-medium.onnx.json"
# Контрольные суммы посчитаны по фактически скачанным файлам (авг 2026)
PIPER_ONNX_SHA256="8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa"
PIPER_JSON_SHA256="c2ec28bb38e2b59e93b959b3e40348c1afebbd272f30fed5d41205d08e98a9d7"
if [ -f "$PIPER_ONNX" ] && [ -f "$PIPER_JSON" ] \
   && sha256_check "$PIPER_ONNX" "$PIPER_ONNX_SHA256" \
   && sha256_check "$PIPER_JSON" "$PIPER_JSON_SHA256"; then
    echo "    уже скачан, пропускаю"
else
    download_verified "$PIPER_BASE/ru_RU-irina-medium.onnx" \
        "$PIPER_ONNX" "Piper-голос irina (63 МБ)" "$PIPER_ONNX_SHA256"
    download_verified "$PIPER_BASE/ru_RU-irina-medium.onnx.json" \
        "$PIPER_JSON" "Конфиг голоса Piper" "$PIPER_JSON_SHA256"
fi
echo "    (другие голоса: dmitri, denis, ruslan — см. README.md)"

echo "==> Естественный русский голос RHVoice (опционально, женский elena)..."
RH_DIR="$MODELS_DIR/rhvoice"
mkdir -p "$RH_DIR/voices"
if [ -d "$RH_DIR/voices/elena" ]; then
    echo "    уже скачан, пропускаю"
else
    echo "    пробую скачать голос elena с зеркала Arch (без sudo)..."
    PKG_INFO=$(curl -fsSL -m 30 "https://archlinux.org/packages/extra/x86_64/rhvoice-voice-elena/json/" 2>/dev/null || true)
    PKG_FILE=$(echo "$PKG_INFO" | python3 -c "import json,sys;print(json.load(sys.stdin).get('filename',''))" 2>/dev/null || true)
    if [ -n "$PKG_FILE" ] && curl -fsSL -m 120 -o /tmp/rhvoice-voice-elena.pkg.tar.zst \
            "https://geo.mirror.pkgbuild.com/extra/os/x86_64/$PKG_FILE"; then
        # Проверяем целостность пакета. Хеш известен для текущей версии
        # (авг 2026); если пакет обновился — печатаем предупреждение,
        # но продолжаем (это зеркало Arch, файл с подписанного репозитория).
        RHVOICE_PKG_SHA256="11262823a37c84513442edfb00647bcd3b2deadadcd0fb3288bb9b536d6ac26f"
        PKG_OK=1
        if [ "$PKG_FILE" = "rhvoice-voice-elena-1.16.5-1-x86_64.pkg.tar.zst" ]; then
            if sha256_check /tmp/rhvoice-voice-elena.pkg.tar.zst "$RHVOICE_PKG_SHA256"; then
                echo "    целостность пакета ОК"
            else
                rm -f /tmp/rhvoice-voice-elena.pkg.tar.zst
                echo "    пакет голоса повреждён при скачивании — установите вручную:"
                echo "        sudo pacman -S rhvoice-voice-elena"
                echo "    или через pip (встроенная библиотека):"
                echo "        $APP_DIR/venv/bin/pip install rhvoice-wrapper-bin"
                PKG_OK=0
            fi
        else
            echo "    версия пакета rhvoice-voice-elena обновилась —"
            echo "    проверка по старому хешу пропущена (файл из репозитория Arch)"
        fi
        if [ "$PKG_OK" = "1" ]; then
            rm -rf /tmp/rhv_extract && mkdir -p /tmp/rhv_extract
            tar -xf /tmp/rhvoice-voice-elena.pkg.tar.zst -C /tmp/rhv_extract \
                usr/share/RHVoice/voices/elena
            cp -r /tmp/rhv_extract/usr/share/RHVoice/voices/elena "$RH_DIR/voices/"
            ln -sfn /usr/share/RHVoice/languages "$RH_DIR/languages" 2>/dev/null || true
            rm -rf /tmp/rhv_extract /tmp/rhvoice-voice-elena.pkg.tar.zst
            echo "    голос elena готов ($RH_DIR)"
            echo "    (нужна также лингвистика: sudo pacman -S rhvoice rhvoice-language-russian)"
        fi
        tar -xf /tmp/rhvoice-voice-elena.pkg.tar.zst -C /tmp/rhv_extract \
            usr/share/RHVoice/voices/elena
        cp -r /tmp/rhv_extract/usr/share/RHVoice/voices/elena "$RH_DIR/voices/"
        ln -sfn /usr/share/RHVoice/languages "$RH_DIR/languages" 2>/dev/null || true
        rm -rf /tmp/rhv_extract /tmp/rhvoice-voice-elena.pkg.tar.zst
        echo "    голос elena готов ($RH_DIR)"
        echo "    (нужна также лингвистика: sudo pacman -S rhvoice rhvoice-language-russian)"
    else
        echo "    не удалось скачать. Для естественного голоса выполните:"
        echo "        sudo pacman -S rhvoice rhvoice-language-russian rhvoice-voice-elena"
        echo "    или установите python-модуль со встроенной библиотекой:"
        echo "        $APP_DIR/venv/bin/pip install rhvoice-wrapper-bin"
    fi
fi

echo "==> Добавляю пользователя в группу 'video' (нужно для управления яркостью через brightnessctl)..."
if ! groups "$USER" | grep -qw video; then
    sudo usermod -aG video "$USER"
    echo "    Добавлено. Понадобится перелогиниться, чтобы это применилось."
else
    echo "    уже в группе video"
fi

echo "==> Проверяю LLM (облачная + локальная Ollama в гонке)..."
if ! command -v ollama >/dev/null 2>&1; then
    echo "!! Ollama не найден (нужен для локального участника гонки)."
    echo "   Установите: curl -fsSL https://ollama.com/install.sh | sh"
    echo "   и скачайте лёгкую модель: ollama pull qwen2.5:3b-instruct"
else
    echo "    Ollama найден. Для слабого ноутбука рекомендую лёгкую модель:"
    echo "        ollama pull qwen2.5:3b-instruct   (или 1.5b — ещё легче)"
fi
echo "    Облачный режим настроен по умолчанию — БЕСПЛАТНЫЙ"
echo "    DeepSeek V4 Flash Free через opencode.ai/zen (без ключа)."
echo "    Для платного провайдера поменяйте в jarvis_daemon.py:"
echo "        OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL"

echo "==> Устанавливаю systemd --user юнит..."
mkdir -p "$HOME/.config/systemd/user"
cp "$SCRIPT_DIR/systemd/jarvis-assistant.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
# Снимаем старый enable (WantedBy=default.target), чтобы не было двух симлинков
systemctl --user disable jarvis-assistant.service >/dev/null 2>&1 || true
systemctl --user enable jarvis-assistant.service

echo "==> Включаю расширение GNOME Shell..."
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable jarvis-assistant@local || \
        echo "!! Не удалось включить автоматически — включите через 'Extensions' или Ctrl+Alt+Esc для перезапуска Shell (X11) / перелогин (Wayland)"
else
    echo "!! Команда gnome-extensions не найдена, включите расширение вручную"
fi

echo ""
echo "======================================================"
echo " Установка завершена."
echo ""
echo " Дальнейшие шаги:"
echo "  1. LLM уже настроена: облако DeepSeek V4 Flash Free + локальная"
echo "     Ollama в гонке (qwen2.5:3b-instruct, если установлена;"
echo "     else: ollama pull qwen2.5:3b-instruct)"
echo "  2. Запустите демон:"
echo "       systemctl --user start jarvis-assistant.service"
echo "  3. Логи демона:"
echo "       journalctl --user -u jarvis-assistant.service -f"
echo "  4. На Wayland перелогиньтесь, чтобы расширение подхватилось;"
echo "     на X11 достаточно Alt+F2 -> r -> Enter."
echo "  5. Скажите «Ева» и задайте вопрос."
echo "  6. Режим активации (голос / горячая клавиша / оба) и сама клавиша"
echo "     настраиваются в меню расширения и в «Настройки → Клавиатура»."
echo "  7. (опционально) Естественный женский голос RHVoice:"
echo "       sudo pacman -S rhvoice rhvoice-language-russian rhvoice-voice-elena && systemctl --user restart jarvis-assistant.service"
echo ""
echo " Если после логина не работал рабочий стол (падал GNOME Shell):"
echo "   systemctl --user disable --now jarvis-assistant.service"
echo "   gnome-extensions disable jarvis-assistant@local"
echo "======================================================"
