import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ai
import bot
import db
import quick


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))


def run_chat(text):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=FakeMessage(text))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={}))
    with mock.patch.object(bot, 'guard', mock.AsyncMock(return_value=True)):
        asyncio.run(bot._chat(update, context))
    return update.message.replies


class QuickParseTests(unittest.TestCase):
    def test_parse_receipts(self):
        r = quick.parse('Recebi 831 do complemento do salário', date(2026, 10, 30))
        self.assertEqual((r['kind'], r['amount'], r['category']), ('receita', '831', 'Salário'))
        self.assertEqual(r['description'], 'Complemento do salário')
        r = quick.parse('Recebi 1.991,19 do contrato municipal', date(2026, 10, 30))
        self.assertEqual((r['kind'], r['amount'], r['occurred_date']), ('receita', '1.991,19', '2026-10-30'))

    def test_parse_expense_and_rejects(self):
        r = quick.parse('Gastei R$ 45,90 no almoço ontem', date(2026, 10, 30))
        self.assertEqual((r['kind'], r['amount'], r['occurred_date']), ('despesa', '45,90', '2026-10-29'))
        self.assertIsNone(quick.parse('Recebi 10 e gastei 20', date(2026, 10, 30)))
        self.assertIsNone(quick.parse('E os valores que já recebi?', date(2026, 10, 30)))


class NeverFakeConfirmTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 'x.sqlite3'
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old
        self.dir.cleanup()

    def _check(self, interp):
        with mock.patch.object(ai, 'interpret_message', interp), \
                mock.patch.object(ai, 'answer', mock.AsyncMock(return_value='✅ Recebido confirmado')) as chat:
            replies = run_chat('Recebi 831 do complemento do salario')
        chat.assert_not_called()
        self.assertEqual(len(replies), 1)
        self.assertIn('831,00', replies[0][0])
        self.assertIsNotNone(replies[0][1])  # botões de confirmação
        self.assertEqual(db.summary()['total'], 0)  # nada gravado sem confirmar

    def test_interpreter_says_chat(self):
        self._check(mock.AsyncMock(return_value={'type': 'chat'}))

    def test_interpreter_says_query(self):
        self._check(mock.AsyncMock(return_value={'type': 'query', 'query': 'debts'}))

    def test_interpreter_times_out(self):
        self._check(mock.AsyncMock(side_effect=RuntimeError('timeout')))

    def test_unparseable_record_never_reaches_chat(self):
        with mock.patch.object(ai, 'interpret_message', mock.AsyncMock(return_value={'type': 'chat'})), \
                mock.patch.object(ai, 'answer', mock.AsyncMock(return_value='✅ confirmado')) as chat:
            replies = run_chat('Recebi 10 e gastei 20')
        chat.assert_not_called()
        self.assertIn('NÃO registrei', replies[0][0])


if __name__ == '__main__':
    unittest.main()


class HistoryShortcutTests(unittest.TestCase):
    def test_detects(self):
        for q in ('ultimas despesas', 'Últimas despesas registradas', 'quais são as últimas receitas', 'últimos gastos com as datas'):
            self.assertIsNotNone(bot.asks_history(q), q)
        self.assertEqual(bot.asks_history('ultimas despesas'), 'despesa')
        self.assertEqual(bot.asks_history('ultimas receitas'), 'receita')
        self.assertIsNone(bot.asks_history('gastei 20 nas ultimas despesas do mes'))

    def test_lists_real_dates_without_ai(self):
        d = tempfile.TemporaryDirectory()
        old = db.DB_PATH
        db.DB_PATH = Path(d.name) / 'h.sqlite3'
        db.init()
        try:
            db.insert_transaction('despesa', 2000, 'Transporte', 'Combustível', '2026-10-27')
            db.insert_transaction('receita', 5000, 'Salário', 'X', '2026-10-28')
            with mock.patch.object(ai, 'answer', mock.AsyncMock()) as chat:
                replies = run_chat('últimas despesas')
            chat.assert_not_called()
            self.assertIn('27/10/2026', replies[0][0])
            self.assertNotIn('Salário', replies[0][0])
            self.assertIn('27/10/2026', db.ai_context())
        finally:
            db.DB_PATH = old
            d.cleanup()
