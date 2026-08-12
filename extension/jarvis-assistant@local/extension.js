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
 * 
 * Примечание: События распознавания речи (Heard) обрабатываются отдельным
 * расширением Dynamic Island.
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


class JarvisAssistantIndicator extends PanelMenu.Button {
    static {
        GObject.registerClass(this);
    }

    _init(settings) {
        super._init(0.0, 'Jarvis Assistant (Ева)', false);

        this._settings = settings;
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
            });
            const id2 = this._proxy.connectSignal('ResponseReady', (proxy, sender, [text]) => {
                this._responseItem.label.text = text;
            });
            this._signalIds = [id1, id2];

            // Подтягиваем текущее состояние сразу после подключения
            const currentState = this._proxy.State || 'idle';
            this._applyStateStyle(currentState);
            const lastResponse = this._proxy.LastResponse;
            if (lastResponse) {
                this._responseItem.label.text = lastResponse;
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
            this._indicator = new JarvisAssistantIndicator(this._settings);
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
            this._indicator = null;
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
