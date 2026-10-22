import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import battery
import bot
import db

LEVELS = [20, 10, 5]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append(text)


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()


class BatteryLogicTests(unittest.TestCase):
    def test_parse_status(self):
        info = battery.parse_status('{"health":"GOOD","percentage":18,"plugged":"UNPLUGGED","status":"DISCHARGING","temperature":31.5}')
        self.assertEqual((info['pct'], info['charging'], info['health']), (18, False, 'GOOD'))
        info = battery.parse_status('{"percentage":55,"plugged":"PLUGGED_AC","status":"CHARGING"}')
        self.assertTrue(info['charging'])
        for bad in ('', 'erro', '{}', '{"percentage":"x"}'):
            with self.assertRaises(ValueError):
                battery.parse_status(bad)

    def test_low_levels_alert_once_each_and_only_downwards(self):
        state = ''
        sent = []
        for pct in (50, 21, 20, 19, 15, 10, 9, 7, 5, 4, 3):
            msg, state = battery.decide(pct, False, state, LEVELS)
            if msg:
                sent.append((pct, state))
        self.assertEqual(sent, [(20, 'low:20'), (10, 'low:10'), (5, 'low:5')])

    def test_first_reading_already_low_alerts_nearest_level(self):
        msg, state = battery.decide(8, False, '', LEVELS)
        self.assertIn('8%', msg)
        self.assertEqual(state, 'low:10')

    def test_charging_resets_so_next_discharge_alerts_again(self):
        msg, state = battery.decide(15, False, '', LEVELS)
        self.assertIsNotNone(msg)
        msg, state = battery.decide(40, True, state, LEVELS)
        self.assertEqual((msg, state), (None, ''))
        msg, state = battery.decide(19, False, state, LEVELS)
        self.assertIsNotNone(msg)

    def test_charging_low_does_not_alert(self):
        self.assertEqual(battery.decide(3, True, '', LEVELS), (None, ''))

    def test_full_alert_once_per_charge_and_can_be_disabled(self):
        msg, state = battery.decide(100, True, '', LEVELS)
        self.assertIn('100%', msg)
        self.assertEqual(battery.decide(100, True, state, LEVELS), (None, 'full'))
        self.assertEqual(battery.decide(100, True, '', LEVELS, full_alert=False), (None, ''))
        msg, state = battery.decide(90, False, 'full', LEVELS)  # tirou do carregador
        self.assertEqual((msg, state), (None, ''))

    def test_describe(self):
        text = battery.describe({'pct': 42, 'charging': True, 'temperature': 30, 'health': 'GOOD'})
        self.assertIn('42% (carregando)', text)
        self.assertIn('Good', text)


class BatteryJobTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        bot._battery_next = 0.0

    def tearDown(self):
        db.DB_PATH = self.old
        self.dir.cleanup()

    def run_job(self, app, info):
        async def fake():
            return info
        with mock.patch.object(battery, 'read_status', fake), mock.patch.object(bot, 'OWNER_ID', 1):
            asyncio.run(bot.battery_job(app))

    def test_job_alerts_and_respects_interval(self):
        app = FakeApp()
        self.run_job(app, {'pct': 18, 'charging': False})
        self.assertEqual(len(app.bot.sent), 1)
        self.assertIn('18%', app.bot.sent[0])
        self.run_job(app, {'pct': 9, 'charging': False})  # dentro dos 5 min: não consulta
        self.assertEqual(len(app.bot.sent), 1)
        bot._battery_next = 0.0
        self.run_job(app, {'pct': 9, 'charging': False})
        self.assertEqual(len(app.bot.sent), 2)
        bot._battery_next = 0.0
        self.run_job(app, {'pct': 9, 'charging': False})  # mesmo nível: sem repetir
        self.assertEqual(len(app.bot.sent), 2)

    def test_job_silent_when_battery_unavailable(self):
        app = FakeApp()
        self.run_job(app, None)
        self.assertEqual(app.bot.sent, [])

    def test_morning_summary_shows_battery(self):
        data = db.morning_data()
        data['battery'] = {'pct': 77, 'charging': False}
        self.assertIn('Bateria do celular: 77%', bot.format_morning(data))
        data['battery'] = None
        self.assertNotIn('Bateria', bot.format_morning(data))

    def test_read_status_without_termux_api_returns_none(self):
        info = asyncio.run(battery.read_status(timeout=2))
        self.assertIsNone(info)  # termux-battery-status não existe neste ambiente



class BatteryCommandTests(unittest.TestCase):
    def fake_command(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / 'termux-battery-status'
        script.write_text('#!/bin/sh\n' + body + '\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return mock.patch.dict(os.environ, {'PATH': tmp.name + os.pathsep + os.environ['PATH']})

    def test_reads_real_command_output(self):
        with self.fake_command('echo \'{"percentage": 42, "plugged": "PLUGGED_USB", "status": "CHARGING"}\''):
            info = asyncio.run(battery.read_status(timeout=5))
        self.assertEqual((info['pct'], info['charging']), (42, True))

    def test_hanging_command_times_out_and_is_killed(self):
        with self.fake_command('sleep 30'):
            info = asyncio.run(battery.read_status(timeout=1))
        self.assertIsNone(info)

    def test_garbage_output_returns_none(self):
        with self.fake_command('echo "Termux:API is not installed"'):
            self.assertIsNone(asyncio.run(battery.read_status(timeout=5)))


if __name__ == '__main__':
    unittest.main()
