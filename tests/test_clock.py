import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import ai
import db
from config import TZ

# Terça-feira, 20/10/2026, 11:17 no fuso configurado
FIXED = datetime(2026, 10, 20, 11, 17, tzinfo=TZ)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.patch = mock.patch.object(ai, 'now', return_value=FIXED)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def test_clock_context_has_weekday_date_and_time(self):
        ctx = ai.clock_context()
        self.assertIn('terça-feira, 20/10/2026, 11:17', ctx)

    def test_direct_question_answered_without_model(self):
        with mock.patch.object(ai, '_request_with_fallback') as llm:
            for q in ('que dia é hoje?', 'Qual a data de hoje?', 'que horas são?', 'Que dia da semana é hoje'):
                out = asyncio.run(ai.answer(q))
                self.assertIn('terça-feira, 20/10/2026', out)
                self.assertIn('11:17', out)
            llm.assert_not_called()

    def test_questions_about_other_dates_still_go_to_model(self):
        for q in ('que dia vence minha conta de luz?', 'qual a data do meu aluguel?',
                  'que dia devo pagar o cartão para não ter juros?'):
            self.assertIsNone(ai.clock_answer(q), q)

    def test_answer_always_sends_clock_even_for_non_financial_text(self):
        cap = {}

        def fake(prompt, messages, mt, temp, force_model=None):
            cap['m'] = messages
            return 'ok', 'rapido', ''
        with mock.patch.object(ai, '_request_with_fallback', side_effect=fake):
            asyncio.run(ai.answer('me conta uma curiosidade'))
        system = [m['content'] for m in cap['m'] if m['role'] == 'system']
        self.assertTrue(any('terça-feira, 20/10/2026, 11:17' in c for c in system))

    def test_interpreter_payload_has_today_weekday_and_now(self):
        cap = {}

        def fake(prompt, messages, mt, temp, force_model=None):
            cap['m'] = messages
            return '{"type":"chat"}', 'rapido', ''
        with mock.patch.object(ai, '_request_with_fallback', side_effect=fake):
            asyncio.run(ai.interpret_message('me lembra amanhã às 9'))
        payload = json.loads(cap['m'][1]['content'])
        self.assertEqual(payload['today'], '2026-10-20')
        self.assertEqual(payload['weekday'], 'terça-feira')
        self.assertTrue(payload['now'].startswith('2026-10-20T11:17'))


if __name__ == '__main__':
    unittest.main()
