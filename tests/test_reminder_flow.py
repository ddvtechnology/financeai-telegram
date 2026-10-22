import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import bot
import db
from config import TZ

NOW = datetime(2026, 10, 22, 7, 58, tzinfo=TZ)


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append(text)


def make_update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=1), message=FakeMessage())


def make_context():
    return SimpleNamespace(application=SimpleNamespace(bot_data={}))


class ReminderFlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.patch = mock.patch.object(db, 'now', lambda: NOW)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def run_flow(self, prompt, model_reminder_at, previous=None, title='Pagar aluguel'):
        update, context = make_update(), make_context()
        result = {'type': 'reminder', 'title': title, 'reminder_at': model_reminder_at}
        asyncio.run(bot.prepare_natural_reminder(update, context, result, prompt, previous))
        actions = list(context.application.bot_data.get('natural_actions', {}).values())
        drafts = context.application.bot_data.get('natural_drafts', {})
        return update, actions, drafts

    def test_user_text_beats_wrong_model_time_and_utc_offset(self):
        # Modelo errou a hora e ainda devolveu UTC: 'às 9h' do usuário prevalece.
        _, actions, _ = self.run_flow('me lembra de pagar aluguel dia 28 às 9h', '2026-10-28T12:00:00Z')
        self.assertEqual(actions[0]['action']['due_at'], datetime(2026, 10, 28, 9, tzinfo=TZ).isoformat())

    def test_relative_time_computed_by_code(self):
        update, actions, _ = self.run_flow('me avisa daqui a 30 minutos de tirar o bolo', '2026-10-22T23:00', title='Tirar o bolo')
        self.assertEqual(actions[0]['action']['due_at'], datetime(2026, 10, 22, 8, 28, tzinfo=TZ).isoformat())
        self.assertIn('daqui a 30 min', update.message.replies[0])
        self.assertIn('quinta-feira', update.message.replies[0])

    def test_model_utc_offset_is_not_converted_when_text_has_no_time(self):
        _, actions, _ = self.run_flow('me lembra de pagar a luz', '2026-10-22T09:00:00+00:00')
        self.assertEqual(actions[0]['action']['due_at'], datetime(2026, 10, 22, 9, tzinfo=TZ).isoformat())

    def test_clarification_answer_is_combined_with_original(self):
        prev = {'original_message': 'me lembra de pagar aluguel amanhã', 'partial': {}}
        _, actions, _ = self.run_flow('às 9 da noite', None, previous=prev)
        self.assertEqual(actions[0]['action']['due_at'], datetime(2026, 10, 23, 21, tzinfo=TZ).isoformat())

    def test_missing_time_asks_instead_of_guessing(self):
        update, actions, drafts = self.run_flow('me lembra de pagar aluguel dia 28', None)
        self.assertEqual(actions, [])
        self.assertIn('Em qual dia e horário', update.message.replies[0])
        self.assertIn(1, drafts)

    def test_past_time_is_rejected(self):
        update, actions, _ = self.run_flow('me lembra hoje às 7h de algo', None)
        self.assertEqual(actions, [])
        self.assertIn('já passaram', update.message.replies[0])

    def test_looks_financial_routes_reminder_phrases(self):
        for text in ('me avisa às 15h', 'me lembre amanhã às 8', 'alarme para as 6h', 'lembrar de pagar'):
            self.assertTrue(bot.looks_financial(text), text)

    def test_due_reminders_compare_real_datetimes(self):
        db.insert_reminder('2026-10-22T07:58:00-03:00', 'agora')            # igual a NOW: vence
        db.insert_reminder('2026-10-22T10:00:00+00:00', 'utc')              # 07:00 local: vence
        db.insert_reminder('2026-10-22T08:30:00-03:00', 'futuro')
        db.insert_reminder('lixo', 'invalido')
        self.assertEqual(sorted(r['content'] for r in db.due_reminders()), ['agora', 'utc'])


if __name__ == '__main__':
    unittest.main()
