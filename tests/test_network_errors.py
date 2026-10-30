import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from telegram.constants import ChatType
from telegram.error import NetworkError, TimedOut
import bot
import db


class FakeQuery:
    def __init__(self, data, fail_answer=False):
        self.data = data
        self.fail_answer = fail_answer
        self.edits = []

    async def answer(self, *a, **k):
        if self.fail_answer:
            raise NetworkError('httpx.ConnectError: ')

    async def edit_message_text(self, text, **k):
        self.edits.append(text)


class NetworkErrorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.owner = mock.patch.object(bot, 'OWNER_ID', 1)
        self.owner.start()

    def tearDown(self):
        self.owner.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def test_safe_answer_swallows_network_error(self):
        asyncio.run(bot.safe_answer(FakeQuery('x', fail_answer=True)))  # não deve lançar

    def test_confirmation_still_saves_when_button_ack_fails(self):
        """Foi o caso do log: query.answer() falhou por rede e a confirmação se perdia."""
        nonce = 'a' * 16
        context = SimpleNamespace(application=SimpleNamespace(bot_data={'natural_actions': {nonce: {
            'created_at': db.now().timestamp(),
            'action': {'type': 'transaction', 'kind': 'despesa', 'amount': 4590, 'category': 'Alimentação',
                       'description': 'Almoço', 'occurred_date': None}}}}))
        query = FakeQuery('nl:confirm:' + nonce, fail_answer=True)
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1),
                                 effective_chat=SimpleNamespace(type=ChatType.PRIVATE))
        asyncio.run(bot.natural_action_callback(update, context))
        self.assertEqual(db.summary()['despesa'], 4590)
        self.assertIn('Lançamento registrado', query.edits[0])

    def test_error_handler_logs_network_errors_briefly(self):
        for err in (NetworkError('httpx.ConnectError: '), TimedOut('Timed out')):
            with self.assertLogs('financeai', level='WARNING') as logs:
                asyncio.run(bot.on_error(None, SimpleNamespace(error=err)))
            self.assertEqual(logs.records[0].levelno, logging.WARNING)
            self.assertIsNone(logs.records[0].exc_info)

    def test_error_handler_keeps_traceback_for_real_bugs(self):
        with self.assertLogs('financeai', level='ERROR') as logs:
            asyncio.run(bot.on_error(None, SimpleNamespace(error=ValueError('bug'))))
        self.assertIsNotNone(logs.records[0].exc_info)


if __name__ == '__main__':
    unittest.main()
