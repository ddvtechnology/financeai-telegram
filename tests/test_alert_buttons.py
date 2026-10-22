import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from telegram.constants import ChatType
import bot
import db
from config import TZ

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)


class FakeQuery:
    def __init__(self, data, text='alerta'):
        self.data = data
        self.edits = []
        self.replies = []
        self.markup_cleared = False
        self.message = SimpleNamespace(text=text, reply_text=self._reply)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **k):
        self.edits.append(text)

    async def edit_message_reply_markup(self, markup=None):
        self.markup_cleared = True

    async def _reply(self, text, reply_markup=None, **k):
        self.replies.append((text, reply_markup))


def update_for(query):
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1),
                           effective_chat=SimpleNamespace(type=ChatType.PRIVATE))


class AlertButtonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.patches = [mock.patch.object(db, 'now', lambda: NOW), mock.patch.object(bot, 'OWNER_ID', 1),
                        mock.patch.object(bot, 'MORNING_HOUR', 8)]
        for p in self.patches:
            p.start()
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={}))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def press(self, data):
        q = FakeQuery(data)
        asyncio.run(bot.alert_callback(update_for(q), self.context))
        return q

    def test_snooze_reminder_one_hour_and_tomorrow(self):
        rid = db.insert_reminder('2026-10-22T09:59:00-03:00', 'Remédio')
        db.mark_reminder(rid)
        q = self.press(f'rm:h1:{rid}')
        self.assertIn('Adiado para 22/10 às 11:00', q.edits[0])
        row = db.get_reminders()[0]
        self.assertEqual(row['due_at'], datetime(2026, 10, 22, 11, tzinfo=TZ).isoformat())
        self.assertIsNone(row['sent_at'])
        self.press(f'rm:dy:{rid}')
        self.assertEqual(db.get_reminders()[0]['due_at'], datetime(2026, 10, 23, 8, tzinfo=TZ).isoformat())

    def test_debt_snooze_creates_reminder(self):
        debt = db.add_debt(10000, '2026-10-22', 'Internet')
        q = self.press(f'da:snz:{debt}')
        self.assertIn('11:00', q.edits[0])
        self.assertEqual(db.get_reminders()[0]['content'], 'Pagar Internet')

    def test_debt_pay_button_asks_confirmation_and_pays_full(self):
        debt = db.add_debt(10000, '2026-10-22', 'Internet')
        q = self.press(f'da:pay:{debt}')
        self.assertTrue(q.markup_cleared)
        text, markup = q.replies[0]
        self.assertIn('R$ 100,00', text)
        self.assertEqual(db.get_debts()[0]['paid_amount'], 0)  # nada é registrado sem confirmar
        confirm = markup.inline_keyboard[0][0].callback_data
        pay_q = FakeQuery(confirm)
        asyncio.run(bot.payment_callback(update_for(pay_q), self.context))
        self.assertEqual(db.get_debts(), [])
        self.assertEqual(db.summary()['despesa'], 10000)

    def test_already_paid_debt(self):
        debt = db.add_debt(10000, '2026-10-22', 'Internet')
        db.pay_debt(debt, 10000)
        q = self.press(f'da:pay:{debt}')
        self.assertIn('já está quitada', q.edits[0])


if __name__ == '__main__':
    unittest.main()
