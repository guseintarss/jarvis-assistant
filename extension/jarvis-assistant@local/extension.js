/*
 * Jarvis Assistant — GNOME Shell extension
 *
 * Отображает индикатор состояния голосового ассистента (Ева) в верхней
 * панели и общается с фоновым демоном (backend/jarvis_daemon.py) через D-Bus.
 *
 * Демон сам постоянно слушает микрофон и слово-активатор «Ева»,
 * поэтому расширение — это в первую очередь UI: индикация состояния
 * (ожидание / слушаю / думаю / говорю) и всплывающее меню с последним
 * ответом ассистента, плюс кнопки ручного управления.
 */

import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.jarvis.Assistant';
const OBJECT_PATH = '/org/jarvis/Assistant';
const EXT_BUS_NAME = 'org.jarvis.Assistant.Extension';
const EXT_OBJECT_PATH = '/org/jarvis/Assistant/Extension';
const KEYBINDING_NAME = 'jarvis-activate';
const MODE_KEY = 'mode';
const HOTKEY_KEY = 'hotkey';

// Интерфейс, который демон вызывает, чтобы управлять окнами. Сделано на
// стороне расширения (а не через wmctrl), потому что Wayland-нативные окна
// невидимы извне — только код внутри GNOME Shell их видит и может
// закрывать/переносить между рабочими столами.
const JarvisExtIface = `<node>
  <interface name="org.jarvis.Assistant.Extension">
    <method name="SetupWorkEnvironment">
      <arg type="a(assis)" direction="in" />
      <arg type="as" direction="in" />
      <arg type="s" direction="out" />
    </method>
    <method name="GetWindowContext">
      <arg type="s" direction="out" />
    </method>
    <method name="CaptureScreen">
      <arg type="s" direction="out" />
    </method>
  </interface>
</node>`;

const JarvisIface = `
<node>
  <interface name="org.jarvis.Assistant">
    <method name="Activate" />
    <method name="Interrupt" />
    <method name="Stop" />
    <method name="TogglePause" />
    <method name="SetActivationMode">
      <arg type="s" direction="in" />
    </method>
    <property name="State" type="s" access="read" />
    <property name="LastResponse" type="s" access="read" />
    <signal name="StateChanged">
      <arg type="s" name="state" />
    </signal>
    <signal name="Heard">
      <arg type="s" name="text" />
    </signal>
    <signal name="ResponseReady">
      <arg type="s" name="text" />
    </signal>
  </interface>
</node>`;

const JarvisProxy = Gio.DBusProxy.makeProxyWrapper(JarvisIface);

const STATE_LABELS = {
    offline: 'Демон не запущен',
    idle: 'Ожидаю слово «Ева»',
    listening: 'Слушаю…',
    thinking: 'Обрабатываю запрос…',
    speaking: 'Отвечаю…',
    paused: 'На паузе',
};

// Подсказки, которые по очереди показываются на острове в ожидании
const IDLE_HINTS = [
    'Скажи «Ева»…',
    'Могу открыть приложения, поставить таймер…',
    'Спроси погоду, курс валют или новости',
    '«Ева, начни день» — утренняя сводка',
    '«Ева, режим исследования» — вкладки и заметки',
];

// Свои «радио»-пункты меню: в GNOME 48+ PopupRadioMenuItem удалён,
// поэтому рисуем кружок-галочку обычным пунктом — работает везде 45-50.
class JarvisRadioItem extends PopupMenu.PopupMenuItem {
    static {
        GObject.registerClass(this);
    }

    _init(text) {
        super._init(text);
        this._check = new St.Icon({
            icon_name: 'object-select-symbolic',
            style_class: 'popup-menu-icon',
            visible: false,
        });
        this.add_child(this._check);
        this._checked = false;
    }

    setChecked(checked) {
        this._checked = checked;
        this._check.visible = checked;
    }

    get checked() {
        return this._checked;
    }
}

// =========================================================================
// «Динамический остров» — всплывающая пилюля в центре экрана (в стиле
// iPhone Dynamic Island). Показывает живое состояние ассистента:
//   слушаю  — зелёные пульсирующие полоски;
//   думаю   — три задумчиво мигающие точки;
//   отвечаю — красноватая волна + начало текста ответа.
// Появляется с анимацией при активации и плавно гаснет в ожидании.
// =========================================================================

const ISLAND_TICK_WAVE = 90;     // мс между кадрами волны
const ISLAND_TICK_THINK = 240;   // мс между кадрами точек
const ISLAND_MAX_LABEL = 60;     // символов ответа на острове
const ISLAND_HINT_INTERVAL = 7000; // мс смены подсказок в ожидании

class JarvisIsland extends St.BoxLayout {
    static {
        GObject.registerClass(this);
    }

    _init() {
        super._init({
            style_class: 'jarvis-island',
            reactive: true,
            visible: false,
            opacity: 0,
        });

        this._state = 'idle';
        this._lastResponse = '';
        this._lastHeard = '';
        this._tick = 0;
        this._timers = [];
        this._callbacks = {};
        this._dismissed = false;
        this._hintIdx = 0;

        // Кнопка «позвать Еву» — микрофон. Работает в любом состоянии:
        // в ожидании — активирует голосовой ввод, во время ответа —
        // начинает новую команду (текущая доиграет/отменится).
        this._micBtn = new St.Button({ style_class: 'jarvis-island-mic' });
        this._micBtn.add_child(new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'jarvis-island-mic-icon',
        }));
        this._micBtn.connect('clicked', () => this._callbacks.activate?.());
        this.add_child(this._micBtn);

        // 5 «звуковых» полосок — слушаю/отвечаю
        this._wave = new St.BoxLayout({ style_class: 'jarvis-island-wave' });
        this._bars = [];
        for (let i = 0; i < 5; i++) {
            const bar = new St.Bin({ style_class: 'jarvis-island-bar' });
            bar.height = 18;
            this._bars.push(bar);
            this._wave.add_child(bar);
        }
        this._wave.visible = false;

        // 3 точки — думаю
        this._dots = new St.BoxLayout({ style_class: 'jarvis-island-dots' });
        this._dotList = [];
        for (let i = 0; i < 3; i++) {
            const dot = new St.Bin({ style_class: 'jarvis-island-dot' });
            this._dotList.push(dot);
            this._dots.add_child(dot);
        }
        this._dots.visible = false;

        // Текстовый блок: маленький заголовок-статус + основная строка
        this._textBox = new St.BoxLayout({
            vertical: true,
            style_class: 'jarvis-island-text',
        });
        this._caption = new St.Label({
            text: '',
            style_class: 'jarvis-island-caption',
        });
        this._label = new St.Label({
            text: '',
            style_class: 'jarvis-island-label',
        });
        this._textBox.add_child(this._caption);
        this._textBox.add_child(this._label);

        // Кнопка остановки — прерывает озвучку/обработку (Interrupt)
        this._stopBtn = new St.Button({ style_class: 'jarvis-island-stop' });
        this._stopBtn.add_child(new St.Icon({
            icon_name: 'media-playback-stop-symbolic',
            style_class: 'jarvis-island-stop-icon',
        }));
        this._stopBtn.connect('clicked', () => this._callbacks.interrupt?.());
        this._stopBtn.visible = false;

        // Кнопка «свернуть» — прячет остров до следующей смены состояния
        this._closeBtn = new St.Button({ style_class: 'jarvis-island-close' });
        this._closeBtn.add_child(new St.Icon({
            icon_name: 'window-close-symbolic',
            style_class: 'jarvis-island-close-icon',
        }));
        this._closeBtn.connect('clicked', () => this._dismiss());
        this._closeBtn.visible = false;

        this.add_child(this._wave);
        this.add_child(this._dots);
        this.add_child(this._textBox);
        this.add_child(this._stopBtn);
        this.add_child(this._closeBtn);
    }

    setCallbacks(callbacks) {
        this._callbacks = callbacks || {};
    }

    _center() {
        const monitor = Main.layoutManager.primaryMonitor;
        const [, natW] = this.get_preferred_width(-1);
        this.x = Math.max(0, Math.round((monitor.width - natW) / 2));
        // Сидим НА верхней кромке экрана, поверх панели (как вырез/нотч
        // Dynamic Island): пилюля перекрывает центр панели, но из-за
        // reactive=false клики «проваливаются» сквозь неё к часам.
        this.y = 6;
    }

    _appear() {
        this.remove_all_transitions();
        this._center();
        if (!this.get_parent())
            Main.layoutManager.addTopChrome(this);
        this.visible = true;
        // Остров лежит поверх центра панели — прячем часы, пока он открыт
        this._hidePanelClock();
        // стартуем «сжатой» — из этого состояния ease её «выпрыгивает»
        this.scale_x = 0.92;
        this.scale_y = 0.92;
        this.ease_property('opacity', 255, {
            duration: 200,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
        this.ease_property('scale_x', 1.0, {
            duration: 260,
            mode: Clutter.AnimationMode.EASE_OUT_BACK,
        });
        this.ease_property('scale_y', 1.0, {
            duration: 260,
            mode: Clutter.AnimationMode.EASE_OUT_BACK,
        });
    }

    _disappear() {
        this.remove_all_transitions();
        this.ease_property('opacity', 0, {
            duration: 180,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                this.visible = false;
                this._restorePanelClock();
            },
        });
    }

    // Пользователь свернул остров сам — ждём следующего состояния
    _dismiss() {
        this._stopTimers();
        this.remove_all_transitions();
        this.visible = false;
        this.opacity = 0;
        this._dismissed = true;
        this._restorePanelClock();
    }

    _hidePanelClock() {
        try {
            const dm = Main.panel?.statusArea?.dateMenu;
            if (dm && dm.container)
                dm.container.visible = false;
        } catch (e) {
        }
    }

    _restorePanelClock() {
        try {
            const dm = Main.panel?.statusArea?.dateMenu;
            if (dm && dm.container)
                dm.container.visible = true;
        } catch (e) {
        }
    }

    _stopTimers() {
        for (const id of this._timers) {
            if (id > 0)
                GLib.source_remove(id);
        }
        this._timers = [];
    }

    // --- «слушаю»: зелёная пульсирующая волна ---
    _startWave() {
        const step = () => {
            this._tick++;
            for (let i = 0; i < this._bars.length; i++) {
                const bar = this._bars[i];
                // сумма двух синусов с разными частотами — живой шум, не «робот»
                const t = this._tick / 5 + i * 0.6;
                let v = 0.35 + 0.65 * (0.7 * Math.abs(Math.sin(t)) +
                                       0.3 * Math.abs(Math.sin(t * 2.13 + i * 1.7)));
                v = Math.max(0.12, Math.min(1.0, v));
                bar.ease_property('scale_y', v, {
                    duration: ISLAND_TICK_WAVE,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            }
            return GLib.SOURCE_CONTINUE;
        };
        this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT,
                                           ISLAND_TICK_WAVE, step));
    }

    // --- «думаю»: точки мигают по очереди ---
    _startDots() {
        const step = () => {
            this._tick++;
            for (let i = 0; i < this._dotList.length; i++) {
                const dot = this._dotList[i];
                // фаза каждого звена сдвинута на треть периода
                let s = 0.3 + 0.7 * Math.abs(Math.sin(
                    (this._tick / 3) + i * (2 * Math.PI / 3)));
                if (this._state !== 'thinking')
                    s = 1.0;
                dot.ease_property('scale_y', s, {
                    duration: ISLAND_TICK_THINK,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
                });
                dot.ease_property('scale_x', s, {
                    duration: ISLAND_TICK_THINK,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
                });
            }
            return GLib.SOURCE_CONTINUE;
        };
        this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT,
                                           ISLAND_TICK_THINK, step));
    }

    // --- «думаю»: бегущие точки после услышанной фразы ---
    _startThinkDots() {
        let n = 0;
        const step = () => {
            n = (n % 3) + 1;
            if (this._state === 'thinking')
                this._label.text = `${this._lastHeard}${'.'.repeat(n)}`;
            return GLib.SOURCE_CONTINUE;
        };
        this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, step));
    }

    // --- ожидание: плавно «дышащая» кнопка-микрофон + смена подсказок ---
    _startIdlePulse() {
        let up = false;
        const step = () => {
            up = !up;
            const s = up ? 1.08 : 0.94;
            this._micBtn.ease_property('scale_x', s, {
                duration: 1200,
                mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
            });
            this._micBtn.ease_property('scale_y', s, {
                duration: 1200,
                mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
            });
            return GLib.SOURCE_CONTINUE;
        };
        this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1200, step));

        this._hintIdx = 0;
        this._label.text = IDLE_HINTS[0];
        this._timers.push(GLib.timeout_add(GLib.PRIORITY_DEFAULT,
                                           ISLAND_HINT_INTERVAL, () => {
            this._hintIdx = (this._hintIdx + 1) % IDLE_HINTS.length;
            if (this._state === 'idle')
                this._label.text = IDLE_HINTS[this._hintIdx];
            return GLib.SOURCE_CONTINUE;
        }));
    }

    setHeard(text) {
        this._lastHeard = text || '';
        if (this._state === 'thinking') {
            this._caption.text = 'Вы сказали:';
            this._label.text = this._lastHeard;
        }
    }

    setResponse(text) {
        this._lastResponse = text || '';
        if (this._state === 'speaking') {
            this._caption.text = 'Ева отвечает';
            this._applySpeakingLabel();
        }
    }

    _applySpeakingLabel() {
        let t = (this._lastResponse || '').replace(/\s+/g, ' ').trim();
        if (t.length > ISLAND_MAX_LABEL)
            t = t.slice(0, ISLAND_MAX_LABEL).trimEnd() + '…';
        this._label.text = t || 'Отвечаю…';
        this._center();
    }

    setState(state) {
        const prev = this._state;
        this._state = state;
        this._stopTimers();
        this.remove_style_class_name(`jarvis-island-${prev}`);
        this.add_style_class_name(`jarvis-island-${state}`);

        const active = state === 'listening' || state === 'thinking' || state === 'speaking';
        this._micBtn.visible = true;
        this._micBtn.scale_x = 1.0;
        this._micBtn.scale_y = 1.0;
        this._stopBtn.visible = active;
        this._closeBtn.visible = active;
        this._wave.visible = state === 'listening' || state === 'speaking';
        this._dots.visible = state === 'thinking';
        this._dismissed = false;

        if (state === 'idle') {
            // Компактная капсула-приглашение: подсказки + пульсирующий
            // микрофон. Появляется плавно при готовности демона.
            this._caption.text = 'Ева на месте';
            this._appear();
            this._startIdlePulse();
            this._center();
        } else if (state === 'listening') {
            this._caption.text = 'Слушаю';
            this._label.text = 'Скажите команду…';
            this._appear();
            this._startWave();
            this._center();
        } else if (state === 'thinking') {
            this._caption.text = this._lastHeard ? 'Вы сказали:' : 'Обрабатываю';
            this._label.text = this._lastHeard || 'Думаю…';
            this._appear();
            this._startDots();
            this._startThinkDots();
            this._center();
        } else if (state === 'speaking') {
            this._caption.text = 'Ева отвечает';
            this._applySpeakingLabel();
            this._appear();
            this._startWave();
            this._center();
        } else {
            // paused / offline — плавно гаснем
            this._disappear();
        }
    }

    destroy() {
        this._stopTimers();
        this.remove_all_transitions();
        this._restorePanelClock();
        if (this.get_parent())
            this.get_parent().remove_child(this);
        super.destroy();
    }
}

class JarvisAssistantIndicator extends PanelMenu.Button {
    static {
        GObject.registerClass(this);
    }

    _init(settings, island) {
        super._init(0.0, 'Jarvis Assistant (Ева)', false);

        this._settings = settings;
        this._island = island;
        this._mode = 'voice';
        this._state = 'offline';
        this._proxy = null;
        this._watcherId = 0;
        this._signalIds = [];
        this._settingsChangedId = 0;

        const box = new St.BoxLayout({
            style_class: 'jarvis-indicator-box',
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._core = new St.Widget({ style_class: 'jarvis-core' });
        box.add_child(this._core);

        this._icon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'system-status-icon',
        });
        box.add_child(this._icon);

        this.add_child(box);
        this._applyStateStyle('offline');

        // Пункт со статусом
        this._statusItem = new PopupMenu.PopupMenuItem(STATE_LABELS.offline, {
            reactive: false,
            can_focus: false,
        });
        this.menu.addMenuItem(this._statusItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Последний ответ
        this._responseItem = new PopupMenu.PopupMenuItem('Пока нет ответов', {
            reactive: false,
            can_focus: false,
        });
        // Перенос строк: в GNOME 48+ клитер-прокси ещё жив, но доступ через
        // set_line_wrap может отсутствовать — пробуем оба варианта.
        try {
            this._responseItem.label.set_line_wrap(true);
        } catch (e) {
            if (this._responseItem.label.clutter_text)
                this._responseItem.label.clutter_text.set_line_wrap(true);
        }
        this._responseItem.add_style_class_name('jarvis-response-item');
        this._responseItem.label.add_style_class_name('jarvis-response-label');
        this.menu.addMenuItem(this._responseItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Ручная активация (без слова-активатора)
        this._activateItem = new PopupMenu.PopupMenuItem('Активировать вручную');
        this._activateItem.connect('activate', () => this._callMethod('Activate'));
        this.menu.addMenuItem(this._activateItem);

        // Пауза / возобновление постоянного прослушивания
        this._pauseItem = new PopupMenu.PopupMenuItem('Пауза / возобновить прослушивание');
        this._pauseItem.connect('activate', () => this._callMethod('TogglePause'));
        this.menu.addMenuItem(this._pauseItem);

        // Режим активации: голос / горячая клавиша / оба
        this._modeButtons = {};
        this._modeItem = new PopupMenu.PopupSubMenuMenuItem('Режим активации');
        for (const [value, label] of [
            ['voice', 'По голосу «Ева»'],
            ['hotkey', 'По горячей клавише'],
            ['both', 'Голос + клавиша'],
        ]) {
            const item = new JarvisRadioItem(label);
            item.connect('activate', () => {
                item.setChecked(true);
                this._settings.set_string(MODE_KEY, value);
            });
            this._modeButtons[value] = item;
            this._modeItem.menu.addMenuItem(item);
        }
        this.menu.addMenuItem(this._modeItem);

        // Прервать текущую озвучку/обработку (демон продолжает работать)
        this._stopItem = new PopupMenu.PopupMenuItem('Прервать ответ');
        this._stopItem.connect('activate', () => this._callMethod('Interrupt'));
        this.menu.addMenuItem(this._stopItem);

        this._watchBus();
        this._applyMode(this._settings.get_string(MODE_KEY));
        this._settingsChangedId = this._settings.connect(
            'changed', (_settings, key) => {
                if (key === MODE_KEY)
                    this._applyMode(this._settings.get_string(MODE_KEY));
                else if (key === HOTKEY_KEY)
                    this._updateKeybinding();
            });
    }

    _applyMode(mode) {
        if (mode === 'hotkey' || mode === 'both' || mode === 'voice')
            this._mode = mode;
        for (const [value, item] of Object.entries(this._modeButtons))
            item.setChecked(value === this._mode);
        this._updateKeybinding();
        this._callMethod('SetActivationMode', this._mode);
    }

    _updateKeybinding() {
        Main.wm.removeKeybinding(KEYBINDING_NAME);
        if (this._mode === 'hotkey' || this._mode === 'both') {
            Main.wm.addKeybinding(
                KEYBINDING_NAME,
                this._settings,
                HOTKEY_KEY,
                Meta.KeyBindingFlags.NONE,
                () => this._callMethod('Activate')
            );
        }
    }

    _watchBus() {
        this._watcherId = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            () => this._onAppeared(),
            () => this._onVanished()
        );
    }

    _onAppeared() {
        try {
            this._proxy = new JarvisProxy(
                Gio.DBus.session,
                BUS_NAME,
                OBJECT_PATH
            );

            const id1 = this._proxy.connectSignal('StateChanged', (proxy, sender, [state]) => {
                this._applyStateStyle(state);
                if (this._island)
                    this._island.setState(state);
            });
            const id2 = this._proxy.connectSignal('Heard', (proxy, sender, [text]) => {
                if (this._island)
                    this._island.setHeard(text);
            });
            const id3 = this._proxy.connectSignal('ResponseReady', (proxy, sender, [text]) => {
                this._responseItem.label.text = text;
                if (this._island)
                    this._island.setResponse(text);
            });
            this._signalIds = [id1, id2, id3];

            // Подтягиваем текущее состояние сразу после подключения
            const currentState = this._proxy.State || 'idle';
            this._applyStateStyle(currentState);
            if (this._island)
                this._island.setState(currentState);
            const lastResponse = this._proxy.LastResponse;
            if (lastResponse) {
                this._responseItem.label.text = lastResponse;
                if (this._island)
                    this._island.setResponse(lastResponse);
            }
            // Сообщаем демону выбранный режим активации
            this._callMethod('SetActivationMode', this._mode);
        } catch (e) {
            logError(e, 'Jarvis Assistant: не удалось подключиться к D-Bus сервису');
            this._applyStateStyle('offline');
        }
    }

    _onVanished() {
        this._proxy = null;
        this._applyStateStyle('offline');
    }

    _callMethod(name, ...args) {
        if (!this._proxy) {
            Main.notify('Jarvis Assistant', 'Демон не запущен. Проверьте systemd-сервис jarvis-assistant.');
            return;
        }
        try {
            this._proxy[`${name}Sync`](...args);
        } catch (e) {
            logError(e, `Jarvis Assistant: ошибка вызова ${name}`);
        }
    }

    _applyStateStyle(state) {
        this._state = state;
        this.remove_style_class_name(`jarvis-state-${this._lastAppliedClass || 'offline'}`);
        this.add_style_class_name(`jarvis-state-${state}`);
        this._lastAppliedClass = state;
        // Защита от вызова до создания пункта меню (иначе падала инциализация)
        if (this._statusItem)
            this._statusItem.label.text = STATE_LABELS[state] || state;
    }

    destroy() {
        if (this._watcherId) {
            Gio.bus_unwatch_name(this._watcherId);
            this._watcherId = 0;
        }
        if (this._proxy) {
            this._signalIds.forEach(id => this._proxy.disconnectSignal(id));
        }
        if (this._settingsChangedId)
            this._settings.disconnect(this._settingsChangedId);
        if (this._island) {
            this._island.destroy();
            this._island = null;
        }
        Main.wm.removeKeybinding(KEYBINDING_NAME);
        super.destroy();
    }
}

// ====================== УПРАВЛЕНИЕ ОКНАМИ (для демона) ======================
// «Начни работу»: демон просит расширение закрыть посторонние окна и
// разложить рабочие приложения по отдельным рабочим столам. Окна ищем по
// WM_CLASS или по имени процесса (/proc/<pid>/comm) — это покрывает и
// XWayland, и нативные Wayland-окна.

function _windowComm(pid) {
    try {
        const file = Gio.File.new_for_path(`/proc/${pid}/comm`);
        const [ok, contents] = file.load_contents(null);
        return ok ? contents.toString().trim() : '';
    } catch (e) {
        return '';
    }
}

function _windowMatches(win, spec) {
    const cls = (win.get_wm_class() || '').toLowerCase();
    if (spec.cls.some(c => cls.includes(c.toLowerCase())))
        return true;
    const comm = _windowComm(win.get_pid());
    return spec.comm.includes(comm);
}

function _findWindow(spec) {
    for (const actor of global.get_window_actors()) {
        const win = actor.meta_window;
        if (win && _windowMatches(win, spec))
            return win;
    }
    return null;
}

function _ensureWorkspace(index) {
    const wsManager = global.workspace_manager;
    try {
        while (wsManager.get_n_workspaces() <= index)
            wsManager.append_new_workspace(false, null);
    } catch (e) {
        log(`Jarvis Assistant: не удалось создать рабочий стол ${index + 1}: ${e}`);
    }
}

export class JarvisWindowManager {
    constructor(owner) {
        this._owner = owner;
        this._connection = null;
        this._regId = 0;
        this._busId = 0;
    }

    start() {
        this._busId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            EXT_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            (connection, name) => this._onAcquired(connection, name),
            () => logError('Jarvis Assistant: не удалось занять D-Bus имя ' + EXT_BUS_NAME),
            null
        );
    }

    _onAcquired(connection) {
        this._connection = connection;
        try {
            const nodeInfo = Gio.DBusNodeInfo.new_for_xml(JarvisExtIface);
            this._regId = connection.register_object(
                EXT_OBJECT_PATH,
                nodeInfo.interfaces[0],
                (conn, sender, objectPath, ifaceName, methodName, parameters, invocation) =>
                    this._handleMethod(methodName, parameters, invocation)
            );
        } catch (e) {
            logError(e, 'Jarvis Assistant: не удалось зарегистрировать D-Bus объект');
        }
    }

    _handleMethod(methodName, parameters, invocation) {
        if (methodName === 'SetupWorkEnvironment') {
            const [layout, keepClasses] = parameters.deep_unpack();
            const specs = layout.map(([cls, comm, ws, name]) => ({ cls, comm, ws, name }));
            this._setupWorkEnvironment(specs, keepClasses, summary => {
                invocation.return_value(new GLib.Variant('(s)', [summary]));
            });
            return true;
        }
        if (methodName === 'GetWindowContext') {
            invocation.return_value(new GLib.Variant('(s)', [this._getWindowContext()]));
            return true;
        }
        if (methodName === 'CaptureScreen') {
            this._captureScreen(path => {
                invocation.return_value(new GLib.Variant('(s)', [path ?? '']));
            });
            return true;
        }
        return false;
    }

    // --- «что открыто»: активное окно + список окон по рабочим столам ---

    _getWindowContext() {
        const parts = [];

        const focus = global.display.get_focus_window();
        if (focus) {
            parts.push(`Активное окно: ${focus.get_title()} (${focus.get_wm_class()})`);
        } else {
            parts.push('Активных окон нет');
        }

        const perWs = new Map();
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win)
                continue;
            if (win.get_window_type() !== Meta.WindowType.NORMAL)
                continue;
            if (win.is_skipped_taskbar())
                continue;
            let ws = 1;
            try {
                ws = (win.get_workspace()?.index() ?? 0) + 1;
            } catch (e) {
            }
            if (!perWs.has(ws))
                perWs.set(ws, []);
            perWs.get(ws).push(`${win.get_title()} (${win.get_wm_class()})`);
        }

        const byWs = [...perWs.entries()].sort((a, b) => a[0] - b[0]);
        if (byWs.length > 0) {
            const entries = byWs.map(([ws, wins]) =>
                `стол ${ws}: ${wins.join(', ')}`);
            parts.push(`Открыты окна: ${entries.join('; ')}`);
        } else {
            parts.push('Открытых окон нет');
        }
        return parts.join('. ');
    }

    // --- скриншот: внутри Shell разрешён Shell.Screenshot (та же
    //     механика, что у PrintScreen, — без порталов и политик) ---

    async _captureScreen(done) {
        const file = Gio.File.new_for_path(`${GLib.get_tmp_dir()}/jarvis_screen.png`);
        try {
            const out = file.replace(null, false, 0, null);
            const shooter = new Shell.Screenshot();
            const [ok] = await shooter.screenshot(false, out);
            try {
                out.close(null);
            } catch (e) {
            }
            done(ok ? file.get_path() : null);
        } catch (e) {
            logError(e, 'Jarvis Assistant: не удалось сделать скриншот');
            done(null);
        }
    }

    _setupWorkEnvironment(specs, keepClasses, done) {
        // 1) Закрываем все обычные окна, кроме рабочих приложений
        let closed = 0;
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win)
                continue;
            if (win.is_attached_dialog())
                continue;
            const type = win.get_window_type();
            if (type !== Meta.WindowType.NORMAL && type !== Meta.WindowType.DIALOG)
                continue;
            const cls = (win.get_wm_class() || '').toLowerCase();
            if (keepClasses.some(k => cls.includes(k.toLowerCase())))
                continue;
            if (specs.some(spec => _windowMatches(win, spec)))
                continue;
            win.delete(global.get_current_time());
            closed++;
        }

        // 2) Ждём появления окон (запуск асинхронный) и раскладываем по столам
        const wsManager = global.workspace_manager;
        const arranged = new Set();
        let attempts = 0;
        const maxAttempts = 50; // 50 x 300 мс = 15 с
        const step = () => {
            attempts++;
            for (const spec of specs) {
                if (arranged.has(spec.name))
                    continue;
                const win = _findWindow(spec);
                if (!win)
                    continue;
                _ensureWorkspace(spec.ws - 1);
                win.move_to_workspace(wsManager.get_workspace_by_index(spec.ws - 1));
                arranged.add(spec.name);
            }

            const remaining = specs.filter(s => !arranged.has(s.name));
            if (remaining.length > 0 && attempts < maxAttempts)
                return GLib.SOURCE_CONTINUE;

            // Переключаемся на стол первого приложения и фокусируем его окно
            const first = specs[0];
            if (first) {
                const win = _findWindow(first);
                if (win) {
                    _ensureWorkspace(first.ws - 1);
                    wsManager.get_workspace_by_index(first.ws - 1).activate(global.get_current_time());
                    win.activate(global.get_current_time());
                }
            }
            done(this._buildSummary(closed, specs, arranged, remaining.length > 0));
            return GLib.SOURCE_REMOVE;
        };

        if (specs.length === 0) {
            done(this._buildSummary(closed, specs, arranged, false));
            return;
        }
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, step);
    }

    _buildSummary(closed, specs, arranged, timedOut) {
        const parts = [];
        if (closed)
            parts.push(`Закрыл ${closed} посторонних окон.`);
        for (const spec of specs) {
            if (arranged.has(spec.name))
                parts.push(`${spec.name} на стол ${spec.ws}.`);
        }
        const missing = specs.filter(s => !arranged.has(s.name));
        if (missing.length > 0)
            parts.push(`Не нашёл окна: ${missing.map(s => s.name).join(', ')}.`);
        return parts.join(' ') || 'Всё готово к работе.';
    }

    stop() {
        if (this._regId && this._connection) {
            try {
                this._connection.unregister_object(this._regId);
            } catch (e) {
            }
        }
        if (this._busId)
            Gio.bus_unown_name(this._busId);
        this._connection = null;
        this._regId = 0;
        this._busId = 0;
    }
}

export default class JarvisAssistantExtension extends Extension {
    enable() {
        try {
            this._settings = this.getSettings('org.gnome.shell.extensions.jarvis-assistant');
            this._island = new JarvisIsland();
            this._indicator = new JarvisAssistantIndicator(this._settings, this._island);
            this._island.setCallbacks({
                activate: () => this._indicator._callMethod('Activate'),
                interrupt: () => this._indicator._callMethod('Interrupt'),
            });
            Main.panel.addToStatusArea(this.uuid, this._indicator);
            this._windowManager = new JarvisWindowManager(this);
            this._windowManager.start();
        } catch (e) {
            // Любая ошибка инициализации не должна ронять GNOME Shell.
            logError(e, 'Jarvis Assistant: ошибка инициализации');
            try {
                this._indicator?.destroy();
            } catch (_e) {
            }
            try {
                this._island?.destroy();
            } catch (_e) {
            }
            this._indicator = null;
            this._island = null;
        }
    }

    disable() {
        try {
            this._windowManager?.stop();
        } catch (e) {
            logError(e, 'Jarvis Assistant: ошибка при отключении оконного сервиса');
        }
        try {
            this._indicator?.destroy();
        } catch (e) {
            logError(e, 'Jarvis Assistant: ошибка при отключении');
        }
        this._indicator = null;
        this._windowManager = null;
    }
}
