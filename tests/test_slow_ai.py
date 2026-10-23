import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import ai
import battery
import bot
import db


def make_server(script):
    """Servidor falso da API: `script(n, body)` devolve (atraso_em_s, content, finish_reason)."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            delay, content, finish = script(len(calls), body)
            time.sleep(delay)
            data = json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': finish}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, calls


class SlowAiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        self.keys = mock.patch.dict(ai.MODELS, {'rapido': ('m1', 'k'), 'avancado': ('m2', 'k')})
        self.keys.start()

    def tearDown(self):
        self.keys.stop()
        db.DB_PATH = self.old
        self.dir.cleanup()

    def serve(self, script):
        server, calls = make_server(script)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        patch = mock.patch.object(ai, 'NVIDIA_URL', f'http://127.0.0.1:{server.server_port}/v1/chat/completions')
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_slow_answer_within_timeout_is_accepted(self):
        calls = self.serve(lambda n, b: (1.5, 'Resposta lenta, mas completa.', 'stop'))
        with mock.patch.object(ai, 'AI_TIMEOUT', 5):
            out = asyncio.run(ai.answer('me conta uma curiosidade'))
        self.assertEqual(out, 'Resposta lenta, mas completa.')
        self.assertEqual(len(calls), 1)

    def test_answer_has_large_token_budget_and_flags_truncation(self):
        calls = self.serve(lambda n, b: (0, 'Texto que foi cortado no mei', 'length'))
        out = asyncio.run(ai.answer('me conta uma curiosidade'))
        self.assertGreaterEqual(calls[0]['max_tokens'], 1000)
        self.assertIn('Texto que foi cortado no mei', out)
        self.assertIn('continue', out)

    def test_complete_answer_has_no_truncation_note(self):
        self.serve(lambda n, b: (0, 'Tudo certo.', 'stop'))
        self.assertEqual(asyncio.run(ai.answer('oi tudo bem')), 'Tudo certo.')

    def test_truncated_json_is_retried_with_bigger_budget(self):
        full = json.dumps({'type': 'chat', 'reply': 'ok'})
        calls = self.serve(lambda n, b: (0, '{"type": "ch', 'length') if n == 1 else (0, full, 'stop'))
        result = asyncio.run(ai.interpret_message('olá'))
        self.assertEqual(result['type'], 'chat')
        self.assertEqual([c['max_tokens'] for c in calls], [700, 1400])

    def test_timeout_retries_once_then_reports_error(self):
        calls = self.serve(lambda n, b: (2.5, 'tarde demais', 'stop'))
        with mock.patch.object(ai, 'AI_TIMEOUT', 1), mock.patch.object(ai, 'time', SimpleNamespace(sleep=lambda s: None)):
            with self.assertRaises(RuntimeError) as ctx:
                ai._http_call('m1', 'k', [{'role': 'user', 'content': 'a'}])
        self.assertIn('tempo excedido', str(ctx.exception))
        self.assertEqual(len(calls), 2)  # 1 tentativa + 1 repetição, não 3


class WaitingTests(unittest.TestCase):
    def test_keeps_typing_and_warns_once_when_slow(self):
        class Chat:
            actions = 0

            async def send_action(self, a):
                Chat.actions += 1

        sent = []

        async def reply_text(text, **k):
            sent.append(text)

        update = SimpleNamespace(message=SimpleNamespace(chat=Chat(), reply_text=reply_text))

        async def scenario():
            waiter = bot.Waiting(update)
            waiter.start()
            await asyncio.sleep(0.45)
            await waiter.stop()
            count = Chat.actions
            await asyncio.sleep(0.2)
            return count, Chat.actions

        with mock.patch.object(bot, 'TYPING_INTERVAL', 0.05), mock.patch.object(bot, 'SLOW_NOTICE_AFTER', 0.2):
            before, after = asyncio.run(scenario())
        self.assertGreaterEqual(before, 5)
        self.assertEqual(before, after)  # parou ao chamar stop()
        self.assertEqual(len(sent), 1)
        self.assertIn('demorando', sent[0])


class BatteryLockTests(unittest.TestCase):
    def test_reads_are_serialized(self):
        active, peak = 0, 0

        async def fake_check(timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return None, 'timeout'

        async def scenario():
            await asyncio.gather(*(battery.check(1) for _ in range(4)))

        with mock.patch.object(battery, '_check', fake_check):
            asyncio.run(scenario())
        self.assertEqual(peak, 1)


if __name__ == '__main__':
    unittest.main()
