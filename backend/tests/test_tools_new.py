"""Тесты новых интентов (ЧАСТЬ 2): время, окна, текст, система,
калькулятор, буфер обмена, сервисы.

Сеть и системные вызовы мокаются: курсы валют, погода, новости, перевод,
xclip, systemd-run, wmctrl, SMTP. Чистые функции (парсер, конвертеры,
напоминания, слоты) тестируются по-настоящему.

Запуск (из каталога backend/):
    python -m unittest tests.test_tools_new -v
"""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from jarvis import intents as intents_mod
from jarvis import policy as policy_mod
from jarvis import config
from jarvis import fastroute
from jarvis.tools import calc, text, time as time_tools
from jarvis.tools.registry import Registry


# ============================== СЛОТЫ =======================================


class TestNewSlots(unittest.TestCase):
    """Регэкспы новых интентов извлекают параметры детерминированно."""

    def test_set_timer(self):
        self.assertEqual(
            intents_mod.extract_slots('set_timer', 'поставь таймер на 5 минут'),
            {'duration': '5 минут'})
        self.assertEqual(
            intents_mod.extract_slots('set_timer',
                                      'таймер на 2 часа 30 минут'),
            {'duration': '2 часа 30 минут'})

    def test_set_alarm(self):
        self.assertEqual(
            intents_mod.extract_slots('set_alarm', 'будильник на 7:30'),
            {'time': '7:30'})
        self.assertEqual(
            intents_mod.extract_slots('set_alarm', 'разбуди в 7 утра'),
            {'hour': '7'})

    def test_set_reminder(self):
        slots = intents_mod.extract_slots(
            'set_reminder', 'напомни мне завтра в 18:00 полить цветы')
        self.assertEqual(slots['time'], '18:00')
        self.assertEqual(slots['day'], 'завтра')
        self.assertEqual(slots['text'], 'полить цветы')

    def test_calculate_slot(self):
        self.assertEqual(
            intents_mod.extract_slots('calculate', 'сколько будет 25 умножить на 37'),
            {'expression': '25 умножить на 37'})

    def test_convert_currency_slot(self):
        slots = intents_mod.extract_slots(
            'convert_currency', 'переведи 100 долларов в евро')
        self.assertEqual(slots['amount'], '100')
        self.assertEqual(slots['from'], 'долларов')
        self.assertEqual(slots['to'], 'евро')

    def test_kill_process_slot(self):
        self.assertEqual(
            intents_mod.extract_slots('kill_process', 'убей процесс 1234'),
            {'pid': '1234'})

    def test_translate_slot(self):
        slots = intents_mod.extract_slots(
            'translate_text', 'переведи привет на английский')
        self.assertEqual(slots['lang'], 'английский')
        self.assertEqual(slots['text'], 'привет')

    def test_change_case_slot(self):
        slots = intents_mod.extract_slots(
            'change_case', 'сделай текст заглавными буквами')
        self.assertEqual(slots['case'], 'заглавными')
        self.assertEqual(slots['text'], 'текст')


# ============================== ВРЕМЯ =======================================


class TestTime(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = config.REMINDERS_DB_PATH
        config.REMINDERS_DB_PATH = os.path.join(self.tmp.name, 'r.db')

    def tearDown(self):
        config.REMINDERS_DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_parse_duration(self):
        self.assertEqual(time_tools._parse_duration('5 минут'), 300)
        self.assertEqual(time_tools._parse_duration('2 часа 30 минут'), 9000)
        self.assertEqual(time_tools._parse_duration('90 секунд'), 90)
        self.assertEqual(time_tools._parse_duration('полчаса'), 1800)
        self.assertIsNone(time_tools._parse_duration('что-то непонятное'))

    def test_reminder_roundtrip(self):
        ok, msg = time_tools.set_reminder(
            time='18:00', day='завтра', text='полить цветы')
        self.assertTrue(ok)
        self.assertIn('№1', msg)
        ok2, msg2 = time_tools.list_reminders()
        self.assertTrue(ok2)
        self.assertIn('полить цветы', msg2)
        ok3, msg3 = time_tools.cancel_reminder('1')
        self.assertTrue(ok3)
        ok4, msg4 = time_tools.list_reminders()
        self.assertTrue(ok4)
        self.assertIn('нет', msg4)

    def test_cancel_all(self):
        time_tools.set_reminder(time='10:00', day='завтра', text='а')
        time_tools.set_reminder(time='11:00', day='завтра', text='б')
        ok, _ = time_tools.cancel_reminder('все')
        self.assertTrue(ok)
        _, msg = time_tools.list_reminders()
        self.assertIn('нет', msg)

    def test_check_time_date(self):
        ok, msg = time_tools.check_time()
        self.assertTrue(ok)
        self.assertRegex(msg, r'\d{2}:\d{2}')
        ok, msg = time_tools.check_date()
        self.assertTrue(ok)
        self.assertIn(str(datetime.date.today().year), msg)

    @mock.patch('jarvis.tools.time._run',
                return_value=(0, '', ''))
    def test_set_timer_systemd(self, run):
        ok, msg = time_tools.set_timer('5 минут')
        self.assertTrue(ok)
        self.assertIn('5 мин', msg)
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ['systemd-run', '--user', '--on-active'])
        self.assertIn('300s', args)

    @mock.patch('jarvis.tools.time._run',
                return_value=(0, '', ''))
    def test_set_alarm_systemd(self, run):
        ok, msg = time_tools.set_alarm(time='7:30')
        self.assertTrue(ok)
        self.assertIn('07:30', msg)
        args = run.call_args[0][0]
        self.assertIn('--on-calendar', args)


# ============================== ТЕКСТ =======================================


class TestText(unittest.TestCase):

    def test_count_words(self):
        ok, msg = text.count_words('привет мир, как дела')
        self.assertTrue(ok)
        self.assertIn('4', msg)

    def test_count_words_empty(self):
        ok, _ = text.count_words('')
        self.assertFalse(ok)

    def test_change_case(self):
        ok, msg = text.change_case(case='заглавными', text='привет мир')
        self.assertTrue(ok)
        self.assertEqual(msg, 'ПРИВЕТ МИР')
        ok, msg = text.change_case(case='нижний', text='Привет Мир')
        self.assertEqual(msg, 'привет мир')

    @mock.patch('jarvis.tools.text.requests.get')
    def test_translate(self, get):
        get.return_value = mock.Mock(
            json=lambda: {'responseData': {'translatedText': 'hello'}},
            raise_for_status=lambda: None)
        ok, msg = text.translate_text(lang='английский', text='привет')
        self.assertTrue(ok)
        self.assertIn('hello', msg)

    @mock.patch('jarvis.tools.text.requests.get',
                side_effect=OSError('no net'))
    def test_translate_offline(self, get):
        ok, msg = text.translate_text(lang='английский', text='привет')
        self.assertFalse(ok)
        self.assertIn('сети', msg.lower())


# ============================== КАЛЬКУЛЯТОР ==================================


class TestCalc(unittest.TestCase):

    def test_calculate_basic(self):
        ok, msg = calc.calculate('25 * 37')
        self.assertTrue(ok)
        self.assertIn('925', msg)

    def test_calculate_words(self):
        ok, msg = calc.calculate('2 плюс 2')
        self.assertTrue(ok)
        self.assertIn('4', msg)
        ok, msg = calc.calculate('сколько будет 25 умножить на 37')
        self.assertTrue(ok)
        self.assertIn('925', msg)

    def test_calculate_percent(self):
        ok, msg = calc.calculate('10 процентов от 200')
        self.assertTrue(ok)
        self.assertIn('20', msg)

    def test_calculate_operator_precedence(self):
        ok, msg = calc.calculate('2 + 2 * 3')
        self.assertTrue(ok)
        self.assertIn('8', msg)
        ok, msg = calc.calculate('(5 + 3) * 2')
        self.assertTrue(ok)
        self.assertIn('16', msg)

    def test_calculate_decimal_comma(self):
        ok, msg = calc.calculate('2,5 + 2,5')
        self.assertTrue(ok)
        self.assertIn('5', msg)

    def test_calculate_rejects_code(self):
        for evil in ('__import__("os").system("rm")',
                     'open("/etc/passwd")',
                     '[1, 2, 3]',
                     '"строка" + "инъекция"'):
            ok, _ = calc.calculate(evil)
            self.assertFalse(ok, evil)

    def test_calculate_div_zero(self):
        ok, _ = calc.calculate('1 / 0')
        self.assertFalse(ok)

    def test_convert_units_length(self):
        ok, msg = calc.convert_units('5', 'километров', 'метры')
        self.assertTrue(ok)
        self.assertIn('5000', msg)

    def test_convert_units_mass(self):
        ok, msg = calc.convert_units('3', 'тонны', 'килограммы')
        self.assertTrue(ok)
        self.assertIn('3000', msg)

    def test_convert_units_temp(self):
        ok, msg = calc.convert_units('0', 'градусов цельсия',
                                     'градусы фаренгейта')
        self.assertTrue(ok)
        self.assertIn('32', msg)

    def test_convert_units_different_groups(self):
        ok, _ = calc.convert_units('5', 'километров', 'килограммы')
        self.assertFalse(ok)

    @mock.patch('jarvis.tools.calc._load_rates',
                return_value=({'USD': 1.0, 'EUR': 0.9, 'RUB': 90.0}, True))
    def test_convert_currency_cached(self, rates):
        ok, msg = calc.convert_currency('100', 'долларов', 'евро')
        self.assertTrue(ok)
        self.assertIn('90.00 EUR', msg)

    @mock.patch('jarvis.tools.calc._load_rates', return_value=({}, False))
    @mock.patch('jarvis.tools.calc._update_rates', return_value=False)
    def test_convert_currency_offline(self, upd, rates):
        ok, msg = calc.convert_currency('100', 'долларов', 'евро')
        self.assertFalse(ok)
        self.assertIn('кэша', msg.lower())


# ============================== ОКНА / СИСТЕМА ===============================


class TestWindowsSystem(unittest.TestCase):

    @mock.patch('jarvis.tools.windows.shutil.which', return_value='/bin/wmctrl')
    @mock.patch('jarvis.tools.windows._run',
                return_value=(0, '', ''))
    def test_minimize(self, run, which):
        from jarvis.tools import windows
        ok, msg = windows.minimize_window('браузера')
        self.assertTrue(ok)
        self.assertIn('add,hidden', ' '.join(run.call_args[0][0]))

    @mock.patch('jarvis.tools.windows.shutil.which', return_value=None)
    def test_wmctrl_missing(self, which):
        from jarvis.tools import windows
        ok, msg = windows.list_windows()
        self.assertFalse(ok)
        self.assertIn('wmctrl', msg)

    def test_kill_process_self_guard(self):
        from jarvis.tools.system import kill_process
        ok, _ = kill_process(str(os.getpid()))
        self.assertFalse(ok)

    def test_kill_process_invalid(self):
        from jarvis.tools.system import kill_process
        ok, _ = kill_process('0')
        self.assertFalse(ok)

    @mock.patch('jarvis.tools.system.os.kill')
    def test_kill_process_ok(self, kill):
        from jarvis.tools.system import kill_process
        ok, msg = kill_process('999999')
        self.assertTrue(ok)
        kill.assert_called_once_with(999999, 15)


# ============================== FAST-РОУТ ===================================


class TestFastRoute(unittest.TestCase):
    """Простые запросы выполняются детерминированно, без нейросети."""

    def test_calculate_routed(self):
        intent_name, slots = fastroute.route('сколько будет 25 умножить на 37')
        self.assertEqual(intent_name, 'calculate')
        self.assertEqual(slots, {'expression': '25 умножить на 37'})

    def test_time_routed(self):
        self.assertEqual(fastroute.route('который час')[0], 'check_time')
        self.assertEqual(fastroute.route('какой сегодня день')[0],
                         'check_date')

    def test_non_fast_returns_none(self):
        self.assertIsNone(fastroute.route('найди файл отчёт'))
        self.assertIsNone(fastroute.route('привет'))
        self.assertIsNone(fastroute.route(''))


# ============================== РЕЕСТР ======================================


class TestRegistryNew(unittest.TestCase):

    def setUp(self):
        self.policy = policy_mod.Policy(data={'tools': {}})

    def test_all_new_tools_registered(self):
        registry = Registry(self.policy)
        names = registry.names()
        for name in ('set_timer', 'set_alarm', 'set_reminder', 'check_time',
                     'check_date', 'list_reminders', 'cancel_reminder',
                     'minimize_window', 'maximize_window', 'close_window',
                     'list_windows', 'switch_window', 'switch_workspace',
                     'count_words', 'change_case', 'translate_text',
                     'system_info', 'check_disk', 'check_battery',
                     'check_network', 'list_processes', 'kill_process',
                     'calculate', 'convert_currency', 'convert_units',
                     'clipboard_copy', 'clipboard_paste', 'clipboard_history',
                     'check_weather', 'check_news', 'send_email',
                     'check_calendar'):
            self.assertIn(name, names)

    def test_high_risk_tools_require_confirmation(self):
        registry = Registry(self.policy)
        for name in ('kill_process', 'send_email'):
            self.assertEqual(self.policy.tool_risk(name), 'high')


if __name__ == '__main__':
    unittest.main()