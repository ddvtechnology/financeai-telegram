import ast
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
import db
import ai


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.old=db.DB_PATH
        db.DB_PATH=Path(self.dir.name)/'new.sqlite3'
        db.init()

    def tearDown(self):
        db.DB_PATH=self.old
        self.dir.cleanup()

    def test_money_formats(self):
        self.assertEqual(db.money_to_cents('1.234,56'),123456)
        self.assertEqual(db.money_to_cents('1234.56'),123456)
        self.assertEqual(db.money_to_cents('1.234'),123400)
        self.assertEqual(db.money_to_cents('0,01'),1)
        with self.assertRaises(ValueError):db.money_to_cents('0')
        with self.assertRaises(ValueError):db.money_to_cents('11,333')
        with self.assertRaises(ValueError):db.money_to_cents('-100')

    def test_income_expense_debt_partial_and_balance(self):
        db.insert_transaction('receita',200000,'Salário')
        db.insert_transaction('despesa',1000,'Transporte')
        d=db.add_debt(18000,(db.now().date()+timedelta(days=3)).isoformat(),'Aluguel')
        self.assertEqual(db.summary()['total'],199000)
        self.assertEqual(db.pay_debt(d,6000),12000)
        self.assertEqual(db.summary()['total'],193000)
        self.assertEqual(db.get_debts()[0]['paid_amount'],6000)
        with self.assertRaises(ValueError):db.pay_debt(d,15000)
        self.assertEqual(db.summary()['total'],193000)
        self.assertEqual(db.pay_debt(d,12000),0)
        self.assertEqual(db.get_debts(),[])
        self.assertEqual(db.summary()['total'],181000)

    def test_reminders_and_modes(self):
        dt=(db.now()+timedelta(minutes=2)).isoformat()
        db.insert_reminder(dt,'Conta a pagar')
        self.assertEqual(len(db.get_reminders()),1)
        self.assertEqual(db.due_reminders(),[])
        self.assertEqual(db.get_mode(),'auto')
        db.set_mode('avancado')
        self.assertEqual(db.get_mode(),'avancado')
        self.assertEqual(ai.choose_model('Quais juros devo priorizar?','auto'),'avancado')
        self.assertEqual(ai.choose_model('Olá, tudo bem?','auto'),'rapido')

    def test_transaction_with_historical_date(self):
        db.insert_transaction('despesa', 4590, 'Alimentação', 'Almoço', '2026-10-17')
        rows = db.get_transactions(1)
        self.assertTrue(rows[0]['occurred_at'].startswith('2026-10-17'))

    def test_json_interpretation_parser(self):
        result = ai._extract_json('```json\n{"type":"transaction","kind":"despesa","amount":"45.90"}\n```')
        self.assertEqual(result['type'], 'transaction')
        self.assertEqual(result['kind'], 'despesa')
        self.assertEqual(result['amount'], '45.90')

    def test_bot_entrypoint_references_defined_natural_handlers(self):
        bot_path = Path(__file__).resolve().parents[1] / 'bot.py'
        source = bot_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(bot_path))
        defined = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        required = {
            'natural_action_callback', 'prepare_natural_transaction',
            'prepare_natural_debt', 'prepare_natural_debt_payment',
            'prepare_natural_reminder', 'dispatch_natural_query',
        }
        self.assertTrue(required.issubset(defined), f'Funções ausentes: {sorted(required - defined)}')
        compile(source, str(bot_path), 'exec')

    def test_persistence(self):
        db.insert_transaction('receita',12345,'Salário')
        self.assertEqual(db.summary()['total'],12345)
        with db.conn() as c:
            check=c.execute('PRAGMA integrity_check').fetchone()[0]
        self.assertEqual(check,'ok')


if __name__=='__main__':unittest.main()
