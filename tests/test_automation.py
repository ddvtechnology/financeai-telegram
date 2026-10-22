import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
import bot
import db
from config import TZ


class FakeBot:
    def __init__(self, fail_html=False):
        self.sent = []
        self.markups = []
        self.fail_html = fail_html

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if self.fail_html and parse_mode == 'HTML':
            from telegram.error import BadRequest
            raise BadRequest("Can't parse entities")
        self.sent.append(('msg', text))
        self.markups.append(reply_markup)

    async def send_document(self, chat_id, doc, filename=None, caption=None):
        self.sent.append(('doc', filename))


class FakeApp:
    def __init__(self, **kw):
        self.bot = FakeBot(**kw)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old
        self.dir.cleanup()

    def at(self, y, m, d, h, mi=0):
        return mock.patch.object(db, 'now', lambda: datetime(y, m, d, h, mi, tzinfo=TZ))

    def test_previous_month(self):
        self.assertEqual(db.previous_month(datetime(2026, 1, 1).date()), '2025-12')
        self.assertEqual(db.previous_month(datetime(2026, 11, 3).date()), '2026-10')

    def test_morning_summary_once_per_day_and_after_hour(self):
        db.add_debt(120000, '2026-10-23', 'Aluguel <b>1.200</b> (x)')
        app = FakeApp()
        with mock.patch.object(bot, 'MORNING_HOUR', 8):
            with self.at(2026, 10, 21, 7, 59):
                asyncio.run(bot.daily_jobs(app))
            self.assertEqual(app.bot.sent, [])
            with self.at(2026, 10, 21, 8, 1):
                asyncio.run(bot.daily_jobs(app))
                asyncio.run(bot.daily_jobs(app))
            msgs = [t for k, t in app.bot.sent if k == 'msg']
            self.assertEqual(len(msgs), 1)
            self.assertIn('Aluguel &lt;b&gt;1.200&lt;/b&gt;', msgs[0])
            self.assertIn('vence em 2 dias', msgs[0])

    def test_monthly_report_sent_once_on_first_days(self):
        db.insert_transaction('despesa', 1000, 'Transporte', 'Uber', '2026-10-10')
        app = FakeApp()
        with mock.patch.object(bot, 'MORNING_HOUR', 8), self.at(2026, 11, 1, 9):
            asyncio.run(bot.daily_jobs(app))
            asyncio.run(bot.daily_jobs(app))
        docs = [n for k, n in app.bot.sent if k == 'doc' and n.startswith('relatorio')]
        self.assertEqual(docs, ['relatorio-10-2026.csv'])
        with mock.patch.object(bot, 'MORNING_HOUR', 8), self.at(2026, 11, 20, 9):
            asyncio.run(bot.daily_jobs(app))
        self.assertEqual(len([1 for k, n in app.bot.sent if k == 'doc' and n.startswith('relatorio')]), 1)

    def test_no_monthly_report_without_transactions_still_marks(self):
        app = FakeApp()
        with mock.patch.object(bot, 'MORNING_HOUR', 8), self.at(2026, 11, 2, 9):
            asyncio.run(bot.daily_jobs(app))
        self.assertEqual(db.get_pref('last_monthly_report'), '2026-10')

    def test_reminder_with_special_chars_is_delivered(self):
        due = (db.now() - timedelta(minutes=10)).isoformat()
        db.insert_reminder(due, 'Pagar conta_de_luz *urgente* R$ 1.200,00 (x)')
        for fail in (False, True):
            app = FakeApp(fail_html=fail)
            with mock.patch.object(bot, 'OWNER_ID', 1):
                asyncio.run(bot.send_due_reminders(app))
            self.assertEqual(len(app.bot.sent), 1, f'fail_html={fail}')
            self.assertIn('conta_de_luz', app.bot.sent[0][1])
            self.assertEqual(db.due_reminders(), [])
            if not fail:
                db.insert_reminder(due, 'Pagar conta_de_luz *urgente* R$ 1.200,00 (x)')

    def test_late_reminder_is_flagged(self):
        due = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)
        text = bot.reminder_text({'id': 1, 'content': 'x', 'due_at': due.isoformat()}, now=due + timedelta(minutes=7))
        self.assertIn('7 min de atraso', text)
        text = bot.reminder_text({'id': 1, 'content': 'x', 'due_at': due.isoformat()}, now=due + timedelta(seconds=20))
        self.assertNotIn('atraso', text)

    def test_debt_alert_text_escapes_and_overdue(self):
        d = {'title': 'Luz - 2.ª via (a_b)', 'days_left': -2, 'due_date': '2026-10-19', 'remaining': 123456}
        text = bot.debt_alert_text(d)
        self.assertIn('R$ 1.234,56', text)
        self.assertIn('2 dias atrás', text)
        self.assertIn('Conta em atraso', text)


    def test_debt_alerts_wait_for_morning_hour(self):
        db.add_debt(10000, '2026-10-24', 'Internet')  # vence em 3 dias
        app = FakeApp()
        with mock.patch.object(bot, 'OWNER_ID', 1), mock.patch.object(bot, 'MORNING_HOUR', 8):
            with self.at(2026, 10, 21, 3):
                asyncio.run(bot.send_debt_alerts(app))
            self.assertEqual(app.bot.sent, [])
            with self.at(2026, 10, 21, 8, 5):
                asyncio.run(bot.send_debt_alerts(app))
                asyncio.run(bot.send_debt_alerts(app))
        self.assertEqual(len(app.bot.sent), 1)
        self.assertIn('Internet', app.bot.sent[0][1])
        self.assertIsNotNone(app.bot.markups[0])

    def test_reminder_message_has_snooze_buttons(self):
        db.insert_reminder((db.now() - timedelta(minutes=1)).isoformat(), 'Remédio')
        app = FakeApp()
        with mock.patch.object(bot, 'OWNER_ID', 1):
            asyncio.run(bot.send_due_reminders(app))
        data = [b.callback_data for row in app.bot.markups[0].inline_keyboard for b in row]
        self.assertEqual(data, [f'rm:h1:{1}', f'rm:dy:{1}'])

    def test_weekly_backup_sent_every_seven_days(self):
        app = FakeApp()
        names = lambda: [n for k, n in app.bot.sent if k == 'doc' and n.startswith('financeai-backup')]
        with mock.patch.object(bot, 'MORNING_HOUR', 8):
            with self.at(2026, 10, 21, 9):
                asyncio.run(bot.daily_jobs(app))
                asyncio.run(bot.daily_jobs(app))
            self.assertEqual(len(names()), 1)
            with self.at(2026, 10, 27, 9):
                asyncio.run(bot.daily_jobs(app))
            self.assertEqual(len(names()), 1)
            with self.at(2026, 10, 28, 9):
                asyncio.run(bot.daily_jobs(app))
            self.assertEqual(len(names()), 2)


if __name__ == '__main__':
    unittest.main()
