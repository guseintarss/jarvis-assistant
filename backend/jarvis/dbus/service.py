"""D-Bus сервис (org.jarvis.Assistant) — совместим с расширением GNOME Shell.

Интерфейс сохранён от предыдущей версии демона (Activate, Interrupt,
Stop, TogglePause, SetActivationMode, State, LastResponse, сигналы
StateChanged/Heard/ResponseReady) плюс добавлен:

    ProcessCommand(s: text) -> s: response
        текстовый запрос в пайплайн; ответ синхронно.

Голосовой движок (engine) подключается опционально: Activate/Interrupt/
Stop/TogglePause/SetActivationMode делегируются ему, голосовой цикл
публикует состояния/сигналы через этот же сервис.

Реализация на Gio (PyGObject): DBusInterfaceSkeleton.new/newv сломаны
в актуальных версиях PyGObject (TypeError), поэтому используется
низкоуровневый register_object_with_closures2 — как в прежней версии
демона (jarvis_daemon.py), проверено на этой же машине.

В демоне подтверждения доступны только голосовому потоку (текстовый
ProcessCommand без голоса отклоняет опасные шаги с объяснением).
"""

import threading

from gi.repository import GLib, Gio  # noqa: E402

DBUS_BUS_NAME = 'org.jarvis.Assistant'
DBUS_OBJECT_PATH = '/org/jarvis/Assistant'
DBUS_INTERFACE_NAME = 'org.jarvis.Assistant'

INTROSPECTION = """
<node>
  <interface name='org.jarvis.Assistant'>
    <method name='Activate' />
    <method name='Interrupt' />
    <method name='Stop' />
    <method name='TogglePause' />
    <method name='SetActivationMode'>
      <arg type='s' direction='in' />
    </method>
    <method name='ProcessCommand'>
      <arg type='s' direction='in' name='text' />
      <arg type='s' direction='out' name='response' />
    </method>
    <property name='State' type='s' access='read' />
    <property name='LastResponse' type='s' access='read' />
    <signal name='StateChanged'>
      <arg type='s' />
    </signal>
    <signal name='Heard'>
      <arg type='s' />
    </signal>
    <signal name='ResponseReady'>
      <arg type='s' />
    </signal>
    <signal name='CommandResult'>
      <arg type='s' />
    </signal>
  </interface>
</node>
"""


class _Service:
    """Обработчик D-Bus-методов (низкоуровневые closures)."""

    def __init__(self, assistant, engine=None):
        self.assistant = assistant
        self.engine = engine
        self._state = 'idle'
        self._last_response = ''
        self._lock = threading.Lock()
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        iface_info = self._node.lookup_interface(DBUS_INTERFACE_NAME)
        self._bus.register_object_with_closures2(
            DBUS_OBJECT_PATH, iface_info,
            self._on_method_call, self._on_get_property, None)

        def _on_name_lost(_conn, _name, error):
            if error is not None:
                print(f'[dbus] не удалось занять имя {DBUS_BUS_NAME}: '
                      f'{error.message} (другой экземпляр уже запущен?)')
                GLib.idle_add(self._quit)

        Gio.bus_own_name_on_connection(
            self._bus, DBUS_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            None, _on_name_lost)
        print(f'[dbus] D-Bus сервис {DBUS_BUS_NAME} опубликован')

        if engine is not None:
            engine.attach(
                process_fn=self._process_voice,
                on_state=self._set_state,
                on_heard=lambda text: self._emit_signal(
                    'Heard', GLib.Variant('(s)', (text[:200],))),
                on_response=self._on_voice_response,
            )
            engine.start()
            print('[dbus] голосовой движок запущен')

    # ------------------------- свойства ------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def last_response(self):
        return self._last_response

    def _set_state(self, new_state):
        if new_state != self._state:
            self._state = new_state
            self._emit_signal('StateChanged', GLib.Variant('(s)', (new_state,)))

    # ------------------------- сигналы --------------------------------------

    def _emit_signal(self, name, variant):
        """Шлёт сигнал. GDBus можно вызывать только из main-потока —
        из рабочих потоков откладываем через idle_add (как в легаси-демоне)."""
        def _emit():
            try:
                self._bus.emit_signal(
                    None, DBUS_OBJECT_PATH, DBUS_INTERFACE_NAME, name, variant)
            except GLib.Error as exc:
                print(f'[dbus] не удалось отправить сигнал {name}: {exc}')
            return False

        if threading.current_thread() is threading.main_thread():
            _emit()
        else:
            GLib.idle_add(_emit)

    # ------------------------- методы ---------------------------------------

    def _on_method_call(self, _conn, _sender, _path, _iface,
                        method_name, params, invocation):
        try:
            if method_name == 'ProcessCommand':
                text = params.unpack()[0]
                # ответ метода — КОРТЕЖ '(s)', а не 's' (иначе
                # g_dbus_method_invocation_return_value_internal упадёт)
                invocation.return_value(
                    GLib.Variant('(s)', (self._process(text),)))
            elif method_name == 'Activate':
                if self.engine is not None:
                    self.engine.manual_activate()
                invocation.return_value(None)
            elif method_name == 'Interrupt':
                if self.engine is not None:
                    self.engine.interrupt()
                invocation.return_value(None)
            elif method_name == 'Stop':
                if self.engine is not None:
                    self.engine.stop()
                invocation.return_value(None)
            elif method_name == 'TogglePause':
                if self.engine is not None:
                    self.engine.toggle_pause()
                invocation.return_value(None)
            elif method_name == 'SetActivationMode':
                mode = params.unpack()[0]
                if self.engine is not None:
                    self.engine.set_mode(mode)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error(
                    DBUS_INTERFACE_NAME, f'Unknown method: {method_name}')
        except Exception as exc:  # noqa: BLE001
            invocation.return_dbus_error(DBUS_INTERFACE_NAME, str(exc))

    def _process(self, text):
        """Текстовый запрос: обработка в этом же потоке (D-Bus-вызов
        блокирующий), состояние/сигналы — в main-поток через idle_add."""
        self._set_state('processing')
        self._emit_signal('Heard', GLib.Variant('(s)', (text[:200],)))
        with self._lock:
            result = self.assistant.process(text)
        self._last_response = result['response']
        self._set_state('idle')
        self._emit_signal('ResponseReady',
                          GLib.Variant('(s)', (result['response'],)))
        self._emit_signal('CommandResult',
                          GLib.Variant('(s)', (result['response'],)))
        return result['response']

    def _process_voice(self, text):
        """Голосовой запрос (вызывается движком из своего потока): тот же
        пайплайн, но Heard уже отправлен движком, а состояния thinking/
        speaking двигает сам движок."""
        self._set_state('processing')
        with self._lock:
            result = self.assistant.process(text)
        self._last_response = result['response']
        self._emit_signal('ResponseReady',
                          GLib.Variant('(s)', (result['response'],)))
        self._emit_signal('CommandResult',
                          GLib.Variant('(s)', (result['response'],)))
        return result

    def _on_voice_response(self, text):
        """Ответ движка готов (расширение показывает текст, движок озвучит)."""
        self._last_response = text

    # ------------------------- свойства извне --------------------------------

    def _on_get_property(self, _conn, _sender, _path, _iface, property_name):
        if property_name == 'State':
            return GLib.Variant('s', self._state)
        if property_name == 'LastResponse':
            return GLib.Variant('s', self._last_response)
        return None

    def _quit(self):
        self._main_loop.quit()


def run_dbus_service(assistant, policy, engine=None):
    """Запускает главный цикл GLib (блокирующий)."""
    service = _Service(assistant, engine=engine)
    service._main_loop = GLib.MainLoop()
    try:
        service._main_loop.run()
    except KeyboardInterrupt:
        service._main_loop.quit()