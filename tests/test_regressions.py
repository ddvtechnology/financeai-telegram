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
        m1 = db.next_month(db.now().strftime('%Y-%m'))
        m2 = db.next_month(m1)
        db.add_recurring('Faculdade', 57000, 10)
        db.add_recurring('Internet', 3500, 5)
        self.assertEqual(len(db.generate_recurring(m1)), 2)
        self.assertEqual(db.generate_recurring(m1), [])          # não duplica no mesmo mês
        self.assertEqual(len(db.generate_recurring(m2)), 2)      # gera no mês seguinte
        titles = {(d['title'], d['due_date']) for d in db.get_debts()}
        self.assertIn(('Faculdade', f'{m1}-10'), titles)
        self.assertIn(('Internet', f'{m2}-05'), titles)
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

        # Testa callback dinâmico de receita fixa (mn:qinc_id:<id>)
        sal15 = next(r for r in db.get_fixed_incomes() if r['amount'] == 93500)
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        msg = SimpleNamespace(reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
        q = SimpleNamespace(data=f'mn:qinc_id:{sal15["id"]}', message=msg, answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
        upd = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                              effective_user=SimpleNamespace(id=1), message=None, callback_query=q)
        with mock.patch.object(bot, 'OWNER_ID', 1):
            asyncio.run(bot.menu_callback(upd, ctx))
            pending = ctx.application.bot_data.get('natural_actions', {})
            self.assertEqual(len(pending), 1)
            act = next(iter(pending.values()))['action']
            self.assertEqual(act['amount'], 93500)
            self.assertEqual(act['kind'], 'receita')

    def test_perfumes_alert_and_photo_handler_and_guided_buttons(self):
        did = db.add_debt(44200, db.now().date().isoformat(), 'Perfumes')
        due_soon = db.get_debts_due_soon()
        self.assertEqual(len(due_soon), 1)
        self.assertEqual(due_soon[0]['remaining'], 10000)
        m_data = db.morning_data()
        self.assertEqual(m_data['debts'][0]['remaining'], 10000)

        # Botão "✅ Já paguei" (da:pay:<id>) em Perfumes prepara R$ 100,00 (e não R$ 442,00)
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        msg = SimpleNamespace(text='Alerta', reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
        q = SimpleNamespace(
            data=f'da:pay:{did}', message=msg, answer=mock.AsyncMock(),
            edit_message_text=mock.AsyncMock(), edit_message_reply_markup=mock.AsyncMock()
        )
        upd = SimpleNamespace(
            effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=1), message=None, callback_query=q
        )
        with mock.patch.object(bot, 'OWNER_ID', 1):
            asyncio.run(bot.alert_callback(upd, ctx))
            pending_pay = ctx.application.bot_data.get('pending_payments', {})
            self.assertEqual(len(pending_pay), 1)
            nonce, (pay_id, pay_amt, _) = next(iter(pending_pay.items()))
            self.assertEqual(pay_id, did)
            self.assertEqual(pay_amt, 10000)

            # Ao confirmar pagamento, exibe botão "💳 Pagar outra conta"
            q_pay = SimpleNamespace(
                data=f'pay:{nonce}', message=msg, answer=mock.AsyncMock(),
                edit_message_text=mock.AsyncMock()
            )
            upd_pay = SimpleNamespace(
                effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                effective_user=SimpleNamespace(id=1), message=None, callback_query=q_pay
            )
            asyncio.run(bot.payment_callback(upd_pay, ctx))
            kwargs = q_pay.edit_message_text.call_args.kwargs
            self.assertIsNotNone(kwargs.get('reply_markup'))

            # Testa photo_handler com mock do interpret_image
            fake_file = SimpleNamespace(download_as_bytearray=mock.AsyncMock(return_value=bytearray(b'fake-jpeg')))
            fake_photo = SimpleNamespace(get_file=mock.AsyncMock(return_value=fake_file))
            msg_photo = SimpleNamespace(
                photo=[fake_photo], caption='Comprovante PIX',
                chat=SimpleNamespace(send_action=mock.AsyncMock()),
                reply_text=mock.AsyncMock()
            )
            upd_photo = SimpleNamespace(
                effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                effective_user=SimpleNamespace(id=1), message=msg_photo, callback_query=None
            )
            fake_vision = {
                'type': 'transaction', 'kind': 'despesa', 'amount': '35.00',
                'category': 'Outros', 'description': 'Plano celular', 'occurred_date': None
            }
            with mock.patch.object(ai, 'interpret_image', mock.AsyncMock(return_value=fake_vision)):
                asyncio.run(bot.photo_handler(upd_photo, ctx))
                self.assertIn('Plano celular', msg_photo.reply_text.call_args.args[0])

    def test_installment_creation_flow(self):
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        msg = SimpleNamespace(text='', reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
        q = SimpleNamespace(data='mn:new_installment', message=msg, answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
        upd = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                              effective_user=SimpleNamespace(id=1), message=None, callback_query=q)

        with mock.patch.object(bot, 'OWNER_ID', 1):
            # 1. Clica no botão "🔢 Dívida parcelada"
            asyncio.run(bot.menu_callback(upd, ctx))
            state = ctx.application.bot_data['edit_debt_state'][1]
            self.assertEqual(state['mode'], 'installment_title')

            # 2. Usuário digita o nome
            msg_user = SimpleNamespace(text='Tênis', reply_text=mock.AsyncMock(), reply_document=mock.AsyncMock())
            upd_text = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                       effective_user=SimpleNamespace(id=1), message=msg_user, callback_query=None)
            asyncio.run(bot.chat(upd_text, ctx))
            self.assertEqual(state['mode'], 'installment_amount')
            self.assertEqual(state['title'], 'Tênis')

            # 3. Usuário digita o valor total (100)
            msg_user.text = '100,00'
            asyncio.run(bot.chat(upd_text, ctx))
            self.assertEqual(state['mode'], 'installment_count')
            self.assertEqual(state['total_amount'], 10000)

            # 4. Usuário digita o número de parcelas (2)
            msg_user.text = '2'
            asyncio.run(bot.chat(upd_text, ctx))
            self.assertEqual(state['mode'], 'installment_due')
            self.assertEqual(state['count'], 2)

            # 5. Usuário digita a data da 1ª parcela (20/11/2026)
            msg_user.text = '20/11/2026'
            asyncio.run(bot.chat(upd_text, ctx))
            # O estado deve ser consumido e a confirmação gerada
            self.assertNotIn(1, ctx.application.bot_data['edit_debt_state'])
            pending = ctx.application.bot_data['natural_actions']
            self.assertEqual(len(pending), 1)
            nonce, entry = next(iter(pending.items()))
            action = entry['action']
            self.assertEqual(action['type'], 'debt_installments')
            self.assertEqual(len(action['installments']), 2)
            self.assertEqual(action['installments'][0]['title'], 'Tênis [1/2]')
            self.assertEqual(action['installments'][0]['amount'], 5000)
            self.assertEqual(action['installments'][0]['due_date'], '2026-11-20')
            self.assertEqual(action['installments'][1]['title'], 'Tênis [2/2]')
            self.assertEqual(action['installments'][1]['amount'], 5000)
            self.assertEqual(action['installments'][1]['due_date'], '2026-12-20')

            # 6. Confirmar registro
            q_confirm = SimpleNamespace(data=f'nl:confirm:{nonce}', message=msg, answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
            upd_confirm = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                          effective_user=SimpleNamespace(id=1), message=None, callback_query=q_confirm)
            asyncio.run(bot.natural_action_callback(upd_confirm, ctx))
            debts = db.get_debts()
            titles = [d['title'] for d in debts]
            self.assertIn('Tênis [1/2]', titles)
            self.assertIn('Tênis [2/2]', titles)

            # 7. Verifica que cada mês no planejamento desconta apenas sua própria parcela (R$ 50,00)
            cp_nov = db.cumulative_planning('2026-11')
            self.assertEqual(cp_nov['flow_total'], 5000)
            cp_dec = db.cumulative_planning('2026-12')
            self.assertEqual(cp_dec['registered_total'], 5000)
            self.assertEqual(cp_dec['opening_balance'], cp_nov['closing_balance'])

    def test_structured_text_parse_and_fake_record_claim_blocked(self):
        # 1. Texto copiado no formato Tipo/Valor/Categoria/Descrição/Data vira transação determinística
        copied = (
            'Tipo: Receita\n'
            'Valor: R$ 60,00\n'
            'Categoria: Renda Extra\n'
            'Descrição: Design\n'
            'Data: 12/11/2026'
        )
        parsed = bot.quick.parse(copied, db.now().date())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['type'], 'transaction')
        self.assertEqual(parsed['kind'], 'receita')
        self.assertEqual(parsed['amount'], '60,00')
        self.assertEqual(parsed['category'], 'Renda Extra')
        self.assertEqual(parsed['description'], 'Design')
        self.assertEqual(parsed['occurred_date'], '2026-11-12')
        self.assertTrue(bot.quick.is_record_attempt(copied))

        # 2. Se ai.answer alucinar que registrou, o guardrail bloqueia e não salva em messages
        fake_hallucination = ai._Text('✅ Receita registrada!\n\n💰 R$ 60,00 | Renda Extra — Design\n\nSaldo do dia atualizado: R$ 1.157,05')
        with mock.patch.object(ai, '_request_with_fallback', return_value=(fake_hallucination, 'rapido', '')):
            reply = asyncio.run(ai.answer('qualquer coisa'))
        self.assertIn('Nenhum registro foi salvo no banco', reply)
        self.assertEqual(db.last_messages(5), [])

        # 3. ai_context destaca a última receita real do banco
        db.insert_transaction('receita', 7000, 'Renda Fixa', 'Sistema Joaninha baby kids', '2026-11-09')
        ctx_text = db.ai_context()
        self.assertIn('Última receita real registrada no banco:', ctx_text)
        self.assertIn('Sistema Joaninha baby kids', ctx_text)

    def test_improvements_1_2_3_4(self):
        # 1. Consultas diretas ao banco sem IA
        today_iso = db.now().date().isoformat()
        tx_inc = db.insert_transaction('receita', 6000, 'Renda Extra', 'Design', today_iso)
        tx_exp = db.insert_transaction('despesa', 4500, 'Saúde', 'Farmácia', today_iso)
        ans_last = bot.direct_db_answer('qual minha ultima receira?')
        self.assertIsNotNone(ans_last)
        self.assertIn('Design', ans_last)
        self.assertIn('60,00', ans_last)
        ans_today = bot.direct_db_answer('quanto gastei hoje?')
        self.assertIn('45,00', ans_today)
        ans_term = bot.direct_db_answer('quanto gastei com farmacia este mes?')
        self.assertIn('45,00', ans_term)

        # 2. Receitas fixas configuráveis
        incomes = db.get_fixed_incomes()
        self.assertEqual(len(incomes), 3)
        inc_id = db.add_fixed_income('Aluguel Kitnet', 50000, 5)
        db.update_fixed_income(inc_id, 55000)
        status = db.fixed_income_status(db.now().strftime('%Y-%m'))
        self.assertEqual(status['expected'], 183500 + 55000)
        db.remove_fixed_income(inc_id)

        # 3. document_handler aceita PDF
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
        fake_file = SimpleNamespace(download_as_bytearray=mock.AsyncMock(return_value=bytearray(b'%PDF-1.4')))
        fake_doc = SimpleNamespace(mime_type='application/pdf', file_size=1024, get_file=mock.AsyncMock(return_value=fake_file))
        msg_doc = SimpleNamespace(
            document=fake_doc, caption='Boleto luz',
            chat=SimpleNamespace(send_action=mock.AsyncMock()),
            reply_text=mock.AsyncMock()
        )
        upd_doc = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                  effective_user=SimpleNamespace(id=1), message=msg_doc, callback_query=None)
        fake_pdf_res = {'type': 'debt', 'amount': '120.00', 'title': 'Conta de Luz', 'due_date': '2026-11-28'}
        with mock.patch.object(ai, 'interpret_image', mock.AsyncMock(return_value=fake_pdf_res)) as m_vis:
            asyncio.run(bot.document_handler(upd_doc, ctx))
            m_vis.assert_called_once()
            self.assertEqual(m_vis.call_args.kwargs['mime_type'], 'application/pdf')
            self.assertIn('Conta de Luz', msg_doc.reply_text.call_args.args[0])

        # 4. Edição de lançamento no histórico (e proteção de pagamento de dívida)
        updated = db.update_transaction(tx_exp, amount=5000, description='Farmácia Popular')
        self.assertEqual(updated['amount'], 5000)
        self.assertEqual(updated['description'], 'Farmácia Popular')
        self.assertEqual(db.get_transaction(tx_inc)['amount'], 6000)
        did = db.add_debt(3000, today_iso, 'Teste')
        db.pay_debt(did, 3000)
        debt_tx = db.get_transactions(1)[0]['id']
        with self.assertRaises(ValueError):
            db.update_transaction(debt_tx, amount=1000)

    def test_date_queries_pagination_and_intramonth_cashflow_planning(self):
        from datetime import datetime
        from config import TZ
        fixed_now = datetime(2026, 11, 23, 21, 0, tzinfo=TZ)
        with mock.patch.object(db, 'now', lambda: fixed_now):
            # 1. Ordenação cronológica real e consulta direta por data ("Somente dia 09/11" -> "Quero sabe oq paguei nesse dia")
            db.insert_transaction('receita', 7000, 'Renda Fixa', 'Sistema Joaninha baby kids', '2026-11-09')
            db.insert_transaction('despesa', 3500, 'Pagamento de dívida', 'Plano celular', '2026-11-09')
            db.insert_transaction('despesa', 7500, 'Alimentação', 'Mercado', '2026-11-10')
            db.insert_transaction('despesa', 749, 'Saúde', 'Poupe Ja', '2026-11-07')
            txs = db.get_transactions(10)
            dates = [t['occurred_at'][:10] for t in txs]
            self.assertEqual(dates, sorted(dates, reverse=True))

            bot_data = {}
            ans1 = bot.direct_db_answer('Somente dia 09/11', bot_data)
            self.assertIsNotNone(ans1)
            self.assertIn('Plano celular', ans1)
            self.assertIn('Sistema Joaninha', ans1)
            ans2 = bot.direct_db_answer('Quero sabe oq paguei nesse dia', bot_data)
            self.assertIsNotNone(ans2)
            self.assertIn('Plano celular', ans2)
            self.assertNotIn('Sistema Joaninha', ans2)

            # 1b. Pergunta direta se conta já foi paga no mês (caso real do celular em 09/11)
            did_cel = db.add_debt(3500, '2026-11-09', 'Plano celular')
            db.pay_debt(did_cel, 3500, occurred_date='2026-11-09')
            ans_cel = bot.direct_db_answer('Gastaria de saber se ja foi pago a conta do celular esse mes', bot_data)
            self.assertIsNotNone(ans_cel)
            self.assertIn('PAGA', ans_cel)
            self.assertIn('09/11/2026', ans_cel)
            self.assertIn('Contas/dívidas JÁ QUITADAS', db.ai_context())

            # 2. Markdown limpa ### e ---
            cleaned = bot.clean_telegram_markdown('---\n### Ação rápida\n• item')
            self.assertNotIn('###', cleaned)
            self.assertNotIn('---', cleaned)
            self.assertIn('*Ação rápida*', cleaned)

            # 3. Fluxo de caixa cronológico detecta falta de caixa antes do salário de 15/12 mesmo com saldo final positivo!
            db.reset_all()
            db.init()
            # Em 23/11 o usuário tem R$ 100,00 em caixa e ainda vai receber salário dia 30/11 (R$ 830,00),
            # e já recebeu R$ 935,00 (dia 15) e R$ 70,00 (dia 10) mas gastou R$ 905,00 em contas/avulsos.
            db.insert_transaction('receita', 7000, 'Renda Fixa', 'Sistema Joaninha baby kids', '2026-11-09')
            db.insert_transaction('receita', 93500, 'Salário', 'Salário (1ª parcela dia 15)', '2026-11-14')
            db.insert_transaction('despesa', 90500, 'Alimentação', 'Gastos do mês', '2026-11-20')
            # Sem contas restantes em 11/2026, mas com R$ 1.100,00 vencendo dia 06/12 (antes do salário de 15/12!)
            db.add_debt(110000, '2026-12-06', 'Aluguel + Faturas Dez')

            # Novembro ainda tem R$ 830,00 do dia 30/11 a entrar -> fecha 11/2026 com 100 + 830 = R$ 930,00
            cp_nov = db.cumulative_planning('2026-11')
            self.assertEqual(cp_nov['opening_balance'], 10000)
            self.assertEqual(cp_nov['income_info']['remaining_fixed'], 83000)
            self.assertEqual(cp_nov['closing_balance'], 93000)
            # E já detecta o alerta de virada de Dezembro!
            self.assertEqual(cp_nov['next_month'], '2026-12')
            self.assertEqual(cp_nov['next_month_min_balance'], -17000)  # 930 - 1100 = -170 em 06/12
            self.assertEqual(cp_nov['next_month_shortage'], 17000)

            # Dezembro: saldo final é POSITIVO (930 + 1835 - 1100 = +1665), mas no dia 06/12 cai para -R$ 170,00!
            cp_dec = db.cumulative_planning('2026-12')
            self.assertEqual(cp_dec['opening_balance'], 93000)
            self.assertEqual(cp_dec['closing_balance'], 166500)
            self.assertEqual(cp_dec['min_balance'], -17000)
            self.assertEqual(cp_dec['min_balance_date'], '2026-12-06')
            self.assertEqual(cp_dec['shortage'], 17000)
            self.assertEqual(cp_dec['before_day15_balance'], -10000)  # -170 em 06/12 + 70 em 10/12 = -100 antes do dia 15

            # 4. Expiração da memória de conversa (mensagens > 2h não entram em last_messages)
            with db.conn() as c:
                c.execute("INSERT INTO messages(role, content, created_at) VALUES('user', 'antiga', '2026-11-20T10:00:00-03:00')")
            db.remember('user', 'recente')
            recent = db.last_messages(6)
            self.assertEqual([m['content'] for m in recent], ['recente'])
            db.clear_messages()
            self.assertEqual(db.last_messages(6), [])

    def test_five_precision_improvements(self):
        from datetime import datetime
        from config import TZ
        fixed_now = datetime(2026, 11, 23, 21, 0, tzinfo=TZ)
        with mock.patch.object(db, 'now', lambda: fixed_now):
            # Setup base: saldo R$ 100 em 23/11, salário dia 30/11 (R$ 830) a entrar, conta de R$ 1.100 em 06/12
            db.insert_transaction('receita', 7000, 'Renda Fixa', 'Sistema Joaninha baby kids', '2026-11-09')
            db.insert_transaction('receita', 93500, 'Salário', 'Salário (1ª parcela dia 15)', '2026-11-14')
            db.insert_transaction('despesa', 90500, 'Alimentação', 'Gastos do mês', '2026-11-20')
            db.add_debt(110000, '2026-12-06', 'Aluguel + Faturas Dez')

            # 1. Receita futura pontual (13º R$ 500 em 30/11) elimina o negativo de -R$ 170 em 06/12!
            exp_id = db.add_expected_income('13º Salário', 50000, '2026-11-30', 'Salário')
            cp_dec = db.cumulative_planning('2026-12')
            self.assertEqual(cp_dec['opening_balance'], 143000)  # 100 + 830 + 500
            self.assertEqual(cp_dec['min_balance'], 33000)       # 1430 - 1100 = +330!
            self.assertEqual(cp_dec['shortage'], 0)
            rec_info = db.receive_expected_income(exp_id)
            self.assertEqual(rec_info['amount'], 50000)
            self.assertEqual(db.get_expected_incomes(pending_only=True), [])

            # 2. Orçamento de gastos avulsos (R$ 400/mês) calcula cenário prudente
            db.set_variable_budget(40000)
            cp_dec_b = db.cumulative_planning('2026-12')
            self.assertEqual(cp_dec_b['variable_budget'], 40000)
            self.assertEqual(cp_dec_b['prudent_closing_balance'], cp_dec_b['closing_balance'] - 40000)
            db.set_variable_budget(0)

            # 3. Teto mensal configurável em qualquer dívida (sem nome fixo no código)
            dent_id = db.add_debt(60000, '2026-11-30', 'Dentista')
            db.set_debt_monthly_cap(dent_id, 15000)
            d_dent = next(d for d in db.get_debts() if d['id'] == dent_id)
            self.assertEqual(db.monthly_due_amount(d_dent), 15000)
            proj_dec = db.project_month_debts('2026-12')
            dent_dec = next(x for x in proj_dec if x['title'] == 'Dentista')
            self.assertEqual(dent_dec['monthly_due'], 15000)

            # 4. Anti-duplicidade: despesa avulsa "Combustível pro carro" detecta ficha aberta "Combustível" e dá baixa via nl:paydebt
            comb_id = db.add_debt(2000, '2026-11-23', 'Combustível')
            match = db.find_matching_open_debt('Combustível pro carro', 'Transporte', 2000)
            self.assertIsNotNone(match)
            self.assertEqual(match['id'], comb_id)
            ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), args=[])
            msg = SimpleNamespace(reply_text=mock.AsyncMock())
            upd = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                  effective_user=SimpleNamespace(id=1), message=msg, callback_query=None)
            asyncio.run(bot.prepare_natural_transaction(
                upd, ctx,
                {'kind': 'despesa', 'amount': '20.00', 'category': 'Transporte', 'description': 'Combustível pro carro'},
                'Gastei 20 de combustível pro carro',
            ))
            nonce = next(iter(ctx.application.bot_data['natural_actions']))
            q_pay = SimpleNamespace(data=f'nl:paydebt:{nonce}', message=msg, answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
            upd_q = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                    effective_user=SimpleNamespace(id=1), message=None, callback_query=q_pay)
            with mock.patch.object(bot, 'OWNER_ID', 1):
                asyncio.run(bot.natural_action_callback(upd_q, ctx))
            self.assertFalse(any(d['id'] == comb_id for d in db.get_debts(open_only=True)))

            # 5. Simulador determinístico ("Posso gastar 200 hoje?" / "Posso parcelar 600 em 3x?") + Conferência de saldo
            sim_ans = bot.direct_db_answer('Posso parcelar 600 em 3x dia 10?', {})
            self.assertIsNotNone(sim_ans)
            self.assertIn('SIMULAÇÃO', sim_ans)
            self.assertIn('3x', sim_ans)

            # Conferência de saldo real
            ctx.application.bot_data['edit_debt_state'] = {1: {'mode': 'reconcile', 'updated_at': fixed_now.timestamp()}}
            cur_bal = db.summary()['total']
            msg_rec = SimpleNamespace(text=f'{(cur_bal + 2500) / 100:.2f}'.replace('.', ','), reply_text=mock.AsyncMock())
            upd_rec = SimpleNamespace(effective_chat=SimpleNamespace(type=bot.ChatType.PRIVATE),
                                      effective_user=SimpleNamespace(id=1), message=msg_rec, callback_query=None)
            with mock.patch.object(bot, 'OWNER_ID', 1):
                asyncio.run(bot.chat(upd_rec, ctx))
            self.assertIn('Ajuste de Saldo', msg_rec.reply_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()


