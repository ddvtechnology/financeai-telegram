import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from telegram.constants import ChatType
import bot
import db
import importar
import when
from config import TZ

NOW = datetime(2026, 10, 30, 8, 0, tzinfo=TZ)


def at(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class DbReviewTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.clock = mock.patch.object(db, 'now', lambda: NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def test_postponing_a_debt_resets_its_alerts(self):
        debt = db.add_debt(10000, '2026-11-02', 'Luz')
        db.mark_debt_notified(debt, '3d')
        db.update_debt(debt, 10000, new_due_date='2026-12-10')
        self.assertFalse(db.was_debt_notified(debt, '3d'))

    def test_alert_catches_up_when_bot_missed_the_3_day_mark(self):
        with mock.patch.object(db, 'now', lambda: NOW - timedelta(days=5)):
            old = db.add_debt(10000, '2026-11-01', 'Aluguel')  # criada há 5 dias; faltam 2 dias hoje
        recent = db.add_debt(10000, '2026-11-01', 'Recém-criada')  # criada hoje: o usuário acabou de ver
        types = {d['title']: d['alert_type'] for d in db.get_debts_due_soon()}
        self.assertEqual(types.get('Aluguel'), '3d')
        self.assertNotIn('Recém-criada', types)
        self.assertIsNotNone(old and recent)

    def test_fixed_bill_with_past_day_starts_next_month(self):
        db.add_recurring('Internet', 10000, 5)  # hoje é dia 30: o dia 5 já passou
        self.assertEqual(db.generate_recurring(), [])
        self.assertEqual(db.get_debts(), [])
        with mock.patch.object(db, 'now', lambda: at(2026, 11, 1, 9)):
            self.assertEqual(len(db.generate_recurring()), 1)
        self.assertEqual(db.get_debts()[0]['due_date'], '2026-11-05')

    def test_fixed_bill_with_future_day_still_generates_this_month(self):
        with mock.patch.object(db, 'now', lambda: at(2026, 10, 2, 9)):
            db.add_recurring('Internet', 10000, 5)
            self.assertEqual(len(db.generate_recurring()), 1)

    def test_absurd_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            db.insert_transaction('despesa', 100, 'Outros', 'x', '0001-01-01')
        with self.assertRaises(ValueError):
            db.add_debt(100, '9999-12-31', 'x')
        db.insert_transaction('despesa', 100, 'Outros', 'x', '2026-10-01')  # normal continua valendo

    def test_importing_twice_does_not_duplicate_debts(self):
        path = Path(self.dir.name) / 'in.json'
        path.write_text(json.dumps({'contas_a_pagar': [{'titulo': 'Luz', 'valor_total': '120,00', 'vencimento': '2026-11-10'}]}))
        importar.import_financial_data(str(path))
        importar.import_financial_data(str(path))
        self.assertEqual(len(db.get_debts()), 1)


class WhenReviewTests(unittest.TestCase):
    def test_hours_plus_minutes_and_half(self):
        base = at(2026, 10, 30, 8)
        self.assertEqual(when.resolve('daqui a 1 hora e 30 minutos', base), at(2026, 10, 30, 9, 30))
        self.assertEqual(when.resolve('daqui a 2 horas e meia', base), at(2026, 10, 30, 10, 30))
        self.assertEqual(when.resolve('em 1 hora e 15 min', base), at(2026, 10, 30, 9, 15))

    def test_plural_article_is_not_a_time(self):
        base = at(2026, 10, 30, 8)
        self.assertIsNone(when.resolve('me lembra de pagar as 2 parcelas dia 10', base))
        self.assertIsNone(when.resolve('pagar as 2 contas amanhã', base))

    def test_real_times_still_work_with_or_without_accent(self):
        base = at(2026, 10, 30, 8)
        self.assertEqual(when.resolve('amanhã às 9', base), at(2026, 10, 31, 9))
        self.assertEqual(when.resolve('amanhã as 9h', base), at(2026, 10, 31, 9))
        self.assertEqual(when.resolve('me lembra amanhã as 9', base), at(2026, 10, 31, 9))


class ChatOrderTests(unittest.TestCase):
    def test_fast_second_message_sees_the_pending_draft(self):
        """Com block=False, a resposta rápida a uma pergunta da IA não pode virar conversa avulsa."""
        dir_ = tempfile.TemporaryDirectory()
        self.addCleanup(dir_.cleanup)
        old = db.DB_PATH
        db.DB_PATH = Path(dir_.name) / 't.sqlite3'
        self.addCleanup(setattr, db, 'DB_PATH', old)
        db.init()
        seen = []

        async def fake_interpret(prompt, pending=None):
            seen.append((prompt, pending is not None))
            if len(seen) == 1:
                await asyncio.sleep(0.3)
                return {'type': 'clarify', 'question': 'Qual o valor?', 'kind': 'despesa'}
            return {'type': 'chat', 'reply': 'ok'}

        def make_update(text):
            async def reply_text(*a, **k):
                pass
            chat = SimpleNamespace(send_action=lambda *a, **k: asyncio.sleep(0))
            return SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                   effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
                                   message=SimpleNamespace(text=text, chat=chat, reply_text=reply_text))

        context = SimpleNamespace(application=SimpleNamespace(bot_data={}))

        async def scenario():
            first = asyncio.create_task(bot.chat(make_update('gastei no mercado'), context))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(bot.chat(make_update('50 reais'), context))
            await asyncio.gather(first, second)

        with mock.patch.object(bot, 'OWNER_ID', 1), mock.patch.object(bot.ai, 'interpret_message', fake_interpret):
            asyncio.run(scenario())
        self.assertEqual(seen, [('gastei no mercado', False), ('50 reais', True)])


class AlertTextTests(unittest.TestCase):
    def test_two_day_alert_is_not_labeled_as_overdue(self):
        base = {'title': 'Aluguel', 'due_date': '2026-11-01', 'remaining': 10000}
        self.assertIn('vencendo em 2 dias', bot.debt_alert_text({**base, 'days_left': 2}))
        self.assertIn('vencendo em 3 dias', bot.debt_alert_text({**base, 'days_left': 3}))
        self.assertIn('amanhã', bot.debt_alert_text({**base, 'days_left': 1}))
        self.assertIn('atraso', bot.debt_alert_text({**base, 'days_left': -1}))
        self.assertNotIn('atraso', bot.debt_alert_text({**base, 'days_left': 2}))


class LogPrivacyTests(unittest.TestCase):
    def test_guardrail_logs_do_not_include_message_text(self):
        source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8')
        self.assertNotIn('%.80s', source)


if __name__ == '__main__':
    unittest.main()
