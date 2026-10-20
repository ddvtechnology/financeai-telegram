import asyncio
import json
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ai
import bot
import db
import importar


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw.get('parse_mode')))


def fake_update(text=''):
    return SimpleNamespace(message=FakeMessage(), effective_chat=SimpleNamespace(type='private'),
                           effective_user=SimpleNamespace(id=1))


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.dir.name) / 't.sqlite3'
        db.init()
        bot.OWNER_ID = 1

    def tearDown(self):
        db.DB_PATH = self.old
        self.dir.cleanup()

    def test_debt_list_escapes_html_and_does_not_crash(self):
        db.add_debt(12000, (db.now().date() + timedelta(days=3)).isoformat(), 'Luz <b>& cia')
        upd = fake_update()
        asyncio.run(bot.debt_list(upd, SimpleNamespace(args=[])))
        text, mode = upd.message.sent[0]
        self.assertEqual(mode, 'HTML')
        self.assertIn('Luz &lt;b&gt;&amp; cia', text)

    def test_delete_transaction_and_protection(self):
        tx = db.insert_transaction('despesa', 1000, 'Teste')
        db.delete_transaction(tx)
        self.assertIsNone(db.get_transaction(tx))
        d = db.add_debt(5000, (db.now().date() + timedelta(days=1)).isoformat(), 'Conta')
        db.pay_debt(d, 1000)
        paid_tx = db.get_transactions(1)[0]['id']
        with self.assertRaises(ValueError):
            db.delete_transaction(paid_tx)
        with self.assertRaises(ValueError):
            db.delete_transaction(99999)

    def test_messages_retention(self):
        for i in range(520):
            db.remember('user', f'm{i}')
        with db.conn() as c:
            self.assertLessEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 501)
        self.assertEqual(db.last_messages(1)[0]['content'], 'm519')

    def test_import_is_idempotent(self):
        data = {'transacoes': [{'tipo': 'despesa', 'valor': '85.50', 'categoria': 'Alimentação',
                                'descricao': 'Supermercado', 'data': '2026-10-12'}]}
        f = Path(self.dir.name) / 'in.json'
        f.write_text(json.dumps(data), encoding='utf-8')
        with mock.patch('builtins.print'):
            importar.import_financial_data(f)
            importar.import_financial_data(f)
        with db.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 1)

    def test_purge_expired(self):
        now = db.now().timestamp()
        store = {'old': {'created_at': now - 9999}, 'new': {'created_at': now}, 'oldpay': (1, 2, now - 9999)}
        bot.purge_expired(store)
        self.assertEqual(list(store), ['new'])

    def test_http_retry_on_429_then_success(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value = ok
        ok.read.return_value = json.dumps({'choices': [{'message': {'content': 'oi'}}]}).encode()
        err = urllib.error.HTTPError('u', 429, 'x', {}, None)
        with mock.patch('urllib.request.urlopen', side_effect=[err, ok]) as m, mock.patch('time.sleep') as sl:
            self.assertEqual(ai._http_call('m', 'k', [{'role': 'user', 'content': 'a'}]), 'oi')
        self.assertEqual(m.call_count, 2)
        sl.assert_called_once_with(1)

    def test_http_gives_up_after_3_attempts(self):
        err = urllib.error.HTTPError('u', 503, 'x', {}, None)
        with mock.patch('urllib.request.urlopen', side_effect=[err, err, err]) as m, mock.patch('time.sleep'):
            with self.assertRaises(RuntimeError):
                ai._http_call('m', 'k', [{'role': 'user', 'content': 'a'}])
        self.assertEqual(m.call_count, 3)

    def test_http_does_not_retry_401(self):
        err = urllib.error.HTTPError('u', 401, 'x', {}, None)
        with mock.patch('urllib.request.urlopen', side_effect=[err]) as m, mock.patch('time.sleep'):
            with self.assertRaisesRegex(RuntimeError, '401'):
                ai._http_call('m', 'k', [{'role': 'user', 'content': 'a'}])
        self.assertEqual(m.call_count, 1)

    def test_recurring_bills_are_idempotent_and_monthly(self):
        db.add_recurring('Faculdade', 57000, 10)
        db.add_recurring('Internet', 3500, 5)
        self.assertEqual(len(db.generate_recurring('2026-11')), 2)
        self.assertEqual(db.generate_recurring('2026-11'), [])          # não duplica no mesmo mês
        self.assertEqual(len(db.generate_recurring('2026-12')), 2)      # gera no mês seguinte
        titles = {(d['title'], d['due_date']) for d in db.get_debts()}
        self.assertIn(('Faculdade', '2026-11-10'), titles)
        self.assertIn(('Internet', '2026-12-05'), titles)
        with self.assertRaises(ValueError):
            db.add_recurring('X', 100, 31)

    def test_clean_telegram_markdown(self):
        sample = '**A receber em Outubro:**\n*   **Complemento salário:** ~R$ 850,00\n* Item de lista\n*dangling'
        cleaned = bot.clean_telegram_markdown(sample)
        self.assertIn('*A receber em Outubro:*', cleaned)
        self.assertIn('• *Complemento salário:*', cleaned)
        self.assertIn('• Item de lista', cleaned)
        self.assertIn('dangling', cleaned)
        self.assertEqual(cleaned.count('*') % 2, 0)

        # Snake_case test
        sc = 'valor_total e user_id'
        cleaned_sc = bot.clean_telegram_markdown(sc)
        self.assertIn(r'valor\_total', cleaned_sc)
        self.assertIn(r'user\_id', cleaned_sc)

    def test_needs_financial_context(self):
        self.assertFalse(ai.needs_financial_context('Oi'))
        self.assertFalse(ai.needs_financial_context('Boa tarde!'))
        self.assertTrue(ai.needs_financial_context('quanto tenho a receber em novembro?'))
        self.assertTrue(ai.needs_financial_context('faltou o restante do salario'))
        self.assertTrue(ai.needs_financial_context('qual o meu saldo?'))

    def test_ai_context_includes_profile_sections(self):
        db.set_profile({
            'renda_fixa': 'Salario CLT R$ 1.870,00',
            'renda_unica': 'Contrato Municipal R$ 1.966,81 em 30/10',
            'previsao_meses': 'Novembro: R$ 1.785,00'
        })
        ctx = db.ai_context()
        self.assertIn('Salario CLT R$ 1.870,00', ctx)
        self.assertIn('Contrato Municipal R$ 1.966,81 em 30/10', ctx)
        self.assertIn('Novembro: R$ 1.785,00', ctx)


if __name__ == '__main__':
    unittest.main()

