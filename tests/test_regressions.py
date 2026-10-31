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

    def test_dispatch_natural_query_blocks_debts_for_income(self):
        upd = fake_update()
        upd.message.text = '935 no dia 15/10 que já recebi, e o complemento 850, no dia 30/10 que ainda falta'
        with mock.patch('ai.answer', return_value='Resposta mockada') as m_ans:
            res = asyncio.run(bot.dispatch_natural_query(upd, SimpleNamespace(), 'debts'))
            self.assertTrue(res)
            m_ans.assert_called_once()

    def test_refund_debt_payment_and_bot_flow(self):
        d = db.add_debt(5000, (db.now().date() + timedelta(days=1)).isoformat(), 'Internet')
        db.pay_debt(d, 2000)
        self.assertEqual(db.get_debts()[0]['paid_amount'], 2000)
        self.assertEqual(db.summary()['despesa'], 2000)
        paid_tx = db.get_transactions(1)[0]['id']

        upd = fake_update()
        upd.message.text = f'/estornar {paid_tx}'
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}))
        asyncio.run(bot.tx_delete(upd, ctx))
        self.assertIn('Estornar este pagamento', upd.message.sent[0][0])
        nonce = next(iter(ctx.application.bot_data['natural_actions']))

        edits = []
        q = SimpleNamespace(data=f'nl:confirm:{nonce}', answer=mock.AsyncMock(),
                            edit_message_text=mock.AsyncMock(side_effect=lambda t, **k: edits.append(t)))
        q_upd = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(type='private'),
                                effective_user=SimpleNamespace(id=1))
        asyncio.run(bot.natural_action_callback(q_upd, ctx))
        self.assertIn('estornado', edits[0])
        self.assertEqual(db.get_debts()[0]['paid_amount'], 0)
        self.assertEqual(db.summary()['despesa'], 0)
        self.assertIsNone(db.get_transaction(paid_tx))

    def test_recurring_list_and_delete(self):
        rid = db.add_recurring('Academia', 9900, 12)
        self.assertEqual(len(db.get_recurring()), 1)
        upd = fake_update()
        asyncio.run(bot.recurring_list(upd, SimpleNamespace()))
        self.assertIn('Academia', upd.message.sent[0][0])
        db.remove_recurring(rid)
        self.assertEqual(db.get_recurring(), [])
        with self.assertRaises(ValueError):
            db.remove_recurring(rid)

    def test_delete_reminder(self):
        rid = db.insert_reminder((db.now() + timedelta(hours=2)).isoformat(), 'Dentista')
        self.assertIsNotNone(db.get_reminder(rid))
        db.delete_reminder(rid)
        self.assertIsNone(db.get_reminder(rid))
        with self.assertRaises(ValueError):
            db.delete_reminder(rid)

    def test_reset_all_clears_database(self):
        db.insert_transaction('receita', 10000, 'Salário')
        db.add_debt(5000, (db.now().date() + timedelta(days=5)).isoformat(), 'Luz')
        db.add_recurring('Internet', 9900, 10)
        db.insert_reminder((db.now() + timedelta(hours=1)).isoformat(), 'Teste')
        db.set_profile({'renda_fixa': '1000'})
        db.reset_all()
        self.assertEqual(db.get_transactions(), [])
        self.assertEqual(db.get_debts(), [])
        self.assertEqual(db.get_recurring(), [])
        self.assertEqual(db.get_reminders(), [])
        self.assertEqual(db.get_profile(), {})

    def test_import_rejects_paid_amount_greater_than_total(self):
        data = {'contas_a_pagar': [{'titulo': 'Cartão', 'valor_total': '100.00',
                                    'valor_pago': '150.00', 'vencimento': '2026-11-10'}]}
        f = Path(self.dir.name) / 'bad.json'
        f.write_text(json.dumps(data), encoding='utf-8')
        with mock.patch('builtins.print'):
            importar.import_financial_data(f)
        self.assertEqual(db.get_debts(open_only=False), [])

    def test_dados_iniciais_import_and_balances(self):
        dados_path = Path(__file__).resolve().parent.parent / 'dados_iniciais.json'
        with mock.patch('builtins.print'):
            importar.import_financial_data(dados_path)
        s = db.summary('2026-10')
        self.assertEqual(s['receita'], 282119)  # 1991,19 + 830,00
        self.assertEqual(s['despesa'], 37500)   # 175,00 + 100,00 + 100,00
        self.assertEqual(s['total'], 244619)    # R$ 2.446,19

        recs = db.get_recurring()
        self.assertEqual(len(recs), 5)
        combustivel = next(r for r in recs if 'Combustível' in r['title'])
        self.assertEqual(combustivel['weekday'], 0)

        debts = db.get_debts()
        roupas = next(d for d in debts if d['title'] == 'Roupas - Ads MultiMarcas')
        self.assertEqual(roupas['original_amount'] - roupas['paid_amount'], 14500)
        perfumes = next(d for d in debts if d['title'] == 'Perfumes')
        self.assertEqual(perfumes['original_amount'] - perfumes['paid_amount'], 44200)
        comb_nov = [d for d in debts if d['title'] == 'Combustível']
        self.assertEqual(len(comb_nov), 5)  # 5 segundas-feiras em nov/2026

    def test_menu_callback_import_initial_flow(self):
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        msg = SimpleNamespace(reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
        q = SimpleNamespace(
            data='mn:import_init',
            message=msg,
            answer=mock.AsyncMock(),
            edit_message_text=mock.AsyncMock(),
        )
        upd = SimpleNamespace(
            effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=bot.OWNER_ID),
            message=None,
            callback_query=q,
        )
        with mock.patch.object(bot, 'OWNER_ID', 1):
            upd.effective_user.id = 1
            asyncio.run(bot.menu_callback(upd, ctx))
            pending = ctx.application.bot_data.get('natural_actions', {})
            self.assertEqual(len(pending), 1)
            nonce = next(iter(pending.keys()))
            q_confirm = SimpleNamespace(
                data=f'nl:confirm:{nonce}',
                message=msg,
                answer=mock.AsyncMock(),
                edit_message_text=mock.AsyncMock(),
            )
            upd_confirm = SimpleNamespace(
                effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                effective_user=SimpleNamespace(id=1),
                message=None,
                callback_query=q_confirm,
            )
            with mock.patch('builtins.print'):
                asyncio.run(bot.natural_action_callback(upd_confirm, ctx))
            self.assertEqual(db.summary('2026-10')['total'], 244619)

    def test_partial_payment_advances_due_date_and_month_filters(self):
        did1 = db.add_debt(44200, '2026-11-30', 'Perfumes')
        db.add_debt(16400, '2026-12-20', 'Cartão Joanna - Tablet [2/3]')
        self.assertEqual(db.get_debt_months(), ['2026-11', '2026-12'])
        self.assertEqual(len(db.get_debts(month='2026-11')), 1)
        # Pagamento parcial em nov/2026 avança vencimento de 2026-11-30 para 2026-12-30
        rem = db.pay_debt(did1, 10000, occurred_date='2026-11-30')
        self.assertEqual(rem, 34200)
        d_updated = next(d for d in db.get_debts() if d['id'] == did1)
        self.assertEqual(d_updated['due_date'], '2026-12-30')
        self.assertEqual(db.get_debt_months(), ['2026-12'])

        # Testa callback de receita rápida (mn:qinc:sal15)
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        msg = SimpleNamespace(reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
        q = SimpleNamespace(data='mn:qinc:sal15', message=msg, answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
        upd = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                              effective_user=SimpleNamespace(id=1), message=None, callback_query=q)
        with mock.patch.object(bot, 'OWNER_ID', 1):
            asyncio.run(bot.menu_callback(upd, ctx))
            pending = ctx.application.bot_data.get('natural_actions', {})
            self.assertEqual(len(pending), 1)
            act = next(iter(pending.values()))['action']
            self.assertEqual(act['amount'], 93500)
            self.assertEqual(act['kind'], 'receita')


if __name__ == '__main__':
    unittest.main()


