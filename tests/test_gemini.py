import asyncio
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
import ai
import db


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 'g.sqlite3'
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old
        self.dir.cleanup()

    def test_mode_accepts_gemini(self):
        db.set_mode('gemini')
        self.assertEqual(db.get_mode(), 'gemini')
        self.assertEqual(ai.choose_model('oi', 'gemini'), 'gemini')

    def test_request_shape_and_parse(self):
        cap = {}

        def fake_open(req, timeout=0):
            cap['req'] = req
            return Resp(json.dumps({'candidates': [{'content': {'parts': [{'text': 'Olá '}, {'text': 'mundo'}]},
                                                    'finishReason': 'STOP'}]}).encode())
        msgs = [{'role': 'system', 'content': 'SYS'}, {'role': 'user', 'content': 'oi'},
                {'role': 'assistant', 'content': 'ola'}, {'role': 'user', 'content': 'tudo?'}]
        with mock.patch.object(ai.urllib.request, 'urlopen', fake_open):
            out = ai._gemini_call('gemini-x', 'KEY123', msgs, 700, 0.1)
        self.assertEqual(out, 'Olá mundo')
        self.assertFalse(out.truncated)
        req = cap['req']
        self.assertNotIn('KEY123', req.full_url)
        self.assertEqual(req.get_header('X-goog-api-key'), 'KEY123')
        self.assertIn('gemini-x:generateContent', req.full_url)
        body = json.loads(req.data)
        self.assertEqual(body['systemInstruction']['parts'][0]['text'], 'SYS')
        self.assertEqual([c['role'] for c in body['contents']], ['user', 'model', 'user'])

    def test_truncated_and_errors(self):
        def trunc(req, timeout=0):
            return Resp(json.dumps({'candidates': [{'content': {'parts': [{'text': 'x'}]},
                                                    'finishReason': 'MAX_TOKENS'}]}).encode())
        with mock.patch.object(ai.urllib.request, 'urlopen', trunc):
            self.assertTrue(ai._gemini_call('m', 'k', [{'role': 'user', 'content': 'a'}]).truncated)

        def blocked(req, timeout=0):
            return Resp(json.dumps({'candidates': [{'finishReason': 'SAFETY'}]}).encode())
        with mock.patch.object(ai.urllib.request, 'urlopen', blocked):
            with self.assertRaises(RuntimeError):
                ai._gemini_call('m', 'k', [{'role': 'user', 'content': 'a'}])

        def forbidden(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, 'x', {}, None)
        with mock.patch.object(ai.urllib.request, 'urlopen', forbidden):
            with self.assertRaisesRegex(RuntimeError, 'Gemini'):
                ai._gemini_call('m', 'k', [{'role': 'user', 'content': 'a'}])
        with self.assertRaisesRegex(RuntimeError, 'GEMINI_API_KEY'):
            ai._gemini_call('m', '', [{'role': 'user', 'content': 'a'}])

    def test_gemini_mode_routes_answer_and_interpreter(self):
        db.set_mode('gemini')
        used = []

        def fake(prompt, messages, mt, temp, force_model=None):
            used.append(force_model or ai.choose_model(prompt, 'gemini'))
            return '{"type":"chat"}', 'gemini', ''
        with mock.patch.object(ai, '_request_with_fallback', side_effect=fake):
            asyncio.run(ai.interpret_message('gastei 10 no mercado'))
            asyncio.run(ai.answer('explique juros compostos'))
        self.assertEqual(used, ['gemini', 'gemini'])

    def test_auto_mode_falls_back_to_gemini_when_nvidia_fails(self):
        db.set_mode('auto')
        with mock.patch.dict(ai.MODELS, {'rapido': ('m1', 'nvkey'), 'avancado': ('m2', 'nvkey'), 'gemini': ('g1', 'gemkey')}), \
                mock.patch.object(ai, '_http_call', side_effect=RuntimeError('API NVIDIA indisponível: HTTP 503.')), \
                mock.patch.object(ai, '_gemini_call', return_value=ai._Text('Salvo pelo Gemini')):
            resp, used, note = ai._request_with_fallback('oi', [{'role': 'user', 'content': 'oi'}], 500, 0.3)
        self.assertEqual((resp, used), ('Salvo pelo Gemini', 'gemini'))
        self.assertIn('alternativo', note)

    def test_system_prompt_has_no_hardcoded_dates_or_amounts(self):
        self.assertNotIn('1.966,81', ai.SYSTEM)
        self.assertNotIn('2.816,81', ai.SYSTEM)
        self.assertNotIn('Outubro/2026', ai.SYSTEM)


if __name__ == '__main__':
    unittest.main()
