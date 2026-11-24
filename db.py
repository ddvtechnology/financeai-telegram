"""Persistência SQLite em centavos, transações ACID e consultas verificáveis."""
import calendar
import contextlib
import re
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from config import DB_PATH, TZ


def now():
    return datetime.now(TZ)


def money_to_cents(value):
    s = str(value).strip().replace('R$', '').replace(' ', '')
    if not s or not re.fullmatch(r'[0-9.,]+', s):
        raise ValueError('Valor inválido. Exemplo: 125,90')
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace('.', '').replace(',', '.')
    elif s.count('.') > 1 or ('.' in s and len(s.rsplit('.', 1)[1]) == 3):
        s = s.replace('.', '')
    try:
        decimal_value = Decimal(s)
        if not decimal_value.is_finite() or decimal_value <= 0 or decimal_value.as_tuple().exponent < -2:
            raise ValueError('Informe valor positivo com até duas casas decimais.')
        cents = int(decimal_value * 100)
    except InvalidOperation:
        raise ValueError('Valor inválido.') from None
    if cents > 10**13:
        raise ValueError('Valor muito alto.')
    return cents


def brl(cents):
    sign = '-' if cents < 0 else ''
    val = abs(int(cents))
    return f'{sign}R$ {val // 100:,}'.replace(',', '.') + f',{val % 100:02d}'


def parse_date(s):
    return datetime.strptime(s, '%d/%m/%Y').date().isoformat()


def short_date(iso):
    return date.fromisoformat(iso[:10]).strftime('%d/%m/%Y')


@contextlib.contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=5000')
    try:
        with c:
            yield c
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('receita','despesa')),
                amount INTEGER NOT NULL CHECK(amount > 0),
                category TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                debt_id INTEGER,
                FOREIGN KEY(debt_id) REFERENCES debts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(occurred_at);
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                original_amount INTEGER NOT NULL CHECK(original_amount > 0),
                paid_amount INTEGER NOT NULL DEFAULT 0 CHECK(paid_amount>=0),
                due_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS debt_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL REFERENCES debts(id),
                transaction_id INTEGER NOT NULL REFERENCES transactions(id),
                amount INTEGER NOT NULL CHECK(amount>0),
                paid_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recurring_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                day INTEGER NOT NULL CHECK(day BETWEEN 0 AND 30),
                active INTEGER NOT NULL DEFAULT 1,
                last_generated TEXT,
                weekday INTEGER
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                due_at TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reminders ON reminders(due_at, sent_at);
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS debt_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(debt_id, alert_type)
            );
            CREATE TABLE IF NOT EXISTS fixed_incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 30),
                category TEXT NOT NULL DEFAULT 'Renda Fixa',
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS expected_incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                expected_date TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Renda Extra',
                received_tx_id INTEGER REFERENCES transactions(id)
            );
        ''')
        debt_cols = {r['name'] for r in c.execute("PRAGMA table_info(debts)")}
        if 'monthly_cap' not in debt_cols:
            c.execute("ALTER TABLE debts ADD COLUMN monthly_cap INTEGER")
        cap_mig = c.execute("SELECT value FROM preferences WHERE key='debts_monthly_cap_migrated'").fetchone()
        if not cap_mig:
            for d_row in c.execute("SELECT id, title, original_amount, paid_amount, monthly_cap FROM debts WHERE monthly_cap IS NULL").fetchall():
                norm_t = re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKD', str(d_row['title']).lower()).encode('ascii', 'ignore').decode('ascii')).strip()
                if norm_t in ('perfumes', 'roupas ads multimarcas'):
                    c.execute("UPDATE debts SET monthly_cap=10000 WHERE id=?", (d_row['id'],))
            c.execute("INSERT OR IGNORE INTO preferences(key, value) VALUES('debts_monthly_cap_migrated', '1')")
        seeded = c.execute("SELECT value FROM preferences WHERE key='fixed_incomes_seeded'").fetchone()
        cnt_inc = c.execute("SELECT COUNT(*) FROM fixed_incomes").fetchone()[0]
        if not seeded and cnt_inc == 0:
            c.executemany(
                "INSERT INTO fixed_incomes(title, amount, day, category) VALUES(?, ?, ?, ?)",
                [
                    ('Sistema Joaninha baby kids', 7000, 10, 'Renda Fixa'),
                    ('Salário (1ª parcela dia 15)', 93500, 15, 'Salário'),
                    ('Salário (parcela fim do mês)', 83000, 30, 'Salário'),
                ],
            )
            c.execute("INSERT OR IGNORE INTO preferences(key, value) VALUES('fixed_incomes_seeded', '1')")
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='recurring_bills'").fetchone()
        if row and ('BETWEEN 1 AND 28' in row['sql'] or 'weekday' not in row['sql']):
            c.executescript('''
                ALTER TABLE recurring_bills RENAME TO _recurring_bills_old;
                CREATE TABLE recurring_bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    day INTEGER NOT NULL CHECK(day BETWEEN 0 AND 30),
                    active INTEGER NOT NULL DEFAULT 1,
                    last_generated TEXT,
                    weekday INTEGER
                );
                INSERT INTO recurring_bills(id, title, amount, day, active, last_generated)
                    SELECT id, title, amount, day, active, last_generated FROM _recurring_bills_old;
                DROP TABLE _recurring_bills_old;
            ''')
        # Ajuste automático: se Combustível (semanal) estava como R$ 80,00 por segunda, corrige para R$ 20,00 por segunda
        c.execute("UPDATE recurring_bills SET amount=2000 WHERE title='Combustível' AND amount=8000 AND weekday=0")
        c.execute("UPDATE debts SET original_amount=2000 WHERE title='Combustível' AND original_amount=8000 AND paid_amount=0")
        c.execute("UPDATE preferences SET value=REPLACE(value, 'Combustível R$ 80', 'Combustível R$ 20') WHERE key='profile_observacoes_planejamento'")


def _check_date_window(day, years=10):
    """Rejeita datas absurdas (ex.: 0001-01-01 ou 9999-12-31) vindas da IA ou de importação."""
    if abs((day - now().date()).days) > 365 * years:
        raise ValueError('Data fora do intervalo permitido (até 10 anos).')


def insert_transaction(kind, amount, category, description='', occurred_date=None):
    if kind not in ('receita', 'despesa') or amount <= 0 or not category.strip():
        raise ValueError('Lançamento inválido')
    stamp = now().isoformat()
    if occurred_date:
        try:
            day = date.fromisoformat(str(occurred_date))
        except ValueError:
            raise ValueError('Data do lançamento inválida.') from None
        _check_date_window(day)
        current = now()
        stamp = datetime.combine(day, current.timetz().replace(tzinfo=None), tzinfo=TZ).isoformat()
    with conn() as c:
        result = c.execute('''INSERT INTO transactions(kind,amount,category,description,occurred_at)
            VALUES(?,?,?,?,?)''', (kind, amount, category.strip()[:70], description.strip()[:250], stamp))
        return result.lastrowid


def add_debt(amount, due_date, title, monthly_cap=None):
    if amount <= 0 or not title.strip():
        raise ValueError('Conta inválida')
    _check_date_window(date.fromisoformat(due_date))
    cap_val = int(monthly_cap) if monthly_cap is not None else None
    if cap_val is None:
        norm = re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKD', title.lower()).encode('ascii', 'ignore').decode('ascii')).strip()
        if norm in ('perfumes', 'roupas ads multimarcas'):
            cap_val = 10000
    with conn() as c:
        cur = c.execute(
            '''INSERT INTO debts(title,original_amount,due_date,created_at,monthly_cap)
               VALUES(?,?,?,?,?)''',
            (title.strip()[:180], amount, due_date, now().isoformat(), cap_val),
        )
        return cur.lastrowid


def _next_month_due(due_iso):
    """Avança uma data AAAA-MM-DD para o mesmo dia do mês seguinte (ajustando fim do mês)."""
    dt = date.fromisoformat(str(due_iso)[:10])
    ny, nm = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    max_d = calendar.monthrange(ny, nm)[1]
    return date(ny, nm, min(dt.day, max_d)).isoformat()


def pay_debt(debt_id, amount, occurred_date=None):
    """Pagamento e despesa ocorrem na MESMA transação, sem dupla contagem.
    Se for pagamento parcial de conta que vence até o mês do pagamento, avança o vencimento para o mês seguinte."""
    if amount <= 0: raise ValueError('O valor precisa ser positivo.')
    stamp = now().isoformat()
    if occurred_date:
        day = date.fromisoformat(str(occurred_date))
        _check_date_window(day)
        current = now()
        stamp = datetime.combine(day, current.timetz().replace(tzinfo=None), tzinfo=TZ).isoformat()
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d: raise ValueError('Dívida não encontrada.')
        pending = d['original_amount'] - d['paid_amount']
        if amount > pending: raise ValueError(f'Valor acima do restante: {brl(pending)}.')
        remaining = pending - amount
        new_due = d['due_date']
        if remaining > 0 and str(d['due_date'])[:7] <= stamp[:7]:
            new_due = _next_month_due(d['due_date'])
            c.execute('DELETE FROM debt_notifications WHERE debt_id=?', (debt_id,))
        c.execute('UPDATE debts SET paid_amount=paid_amount+?, due_date=? WHERE id=?', (amount, new_due, debt_id))
        tx = c.execute('''INSERT INTO transactions(kind,amount,category,description,occurred_at,debt_id)
            VALUES ('despesa',?,?,?,?,?)''', (amount, 'Pagamento de dívida', d['title'], stamp, debt_id)).lastrowid
        c.execute('''INSERT INTO debt_payments(debt_id,transaction_id,amount,paid_at)
            VALUES (?,?,?,?)''', (debt_id,tx,amount,stamp))
        return remaining


def get_debts(open_only=True, month=None):
    clauses = []
    params = []
    if open_only:
        clauses.append('original_amount > paid_amount')
    if month:
        clauses.append('substr(due_date, 1, 7) = ?')
        params.append(month)
    sql = 'SELECT * FROM debts'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY due_date, id'
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params)]


def get_debt_months():
    """Lista ordenada de meses (AAAA-MM) que possuem contas em aberto."""
    with conn() as c:
        rows = c.execute('''SELECT DISTINCT substr(due_date, 1, 7) AS m FROM debts
                            WHERE original_amount > paid_amount ORDER BY m''').fetchall()
    return [r['m'] for r in rows if r['m']]


def monthly_due_amount(d):
    """Retorna o valor efetivo a pagar no mês para a conta `d`, respeitando `monthly_cap` configurável."""
    rem = d['original_amount'] - d['paid_amount']
    cap = d.get('monthly_cap')
    if cap is not None:
        cap_i = int(cap)
        if cap_i > 0 and rem > cap_i:
            return cap_i
        return rem
    norm = re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKD', str(d['title']).lower()).encode('ascii', 'ignore').decode('ascii')).strip()
    if norm in ('perfumes', 'roupas ads multimarcas') and rem > 10000:
        return 10000
    return rem


def set_debt_monthly_cap(debt_id, cap_cents):
    """Define o teto mensal (R$/mês) para pagamento parcelado contínuo de uma dívida (0 ou None remove o teto)."""
    val = int(cap_cents) if cap_cents and int(cap_cents) > 0 else 0
    with conn() as c:
        d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d:
            raise ValueError(f'Dívida #{debt_id} não encontrada.')
        c.execute('UPDATE debts SET monthly_cap=? WHERE id=?', (val, debt_id))
        return dict(d, monthly_cap=val)


def find_matching_open_debt(description, category='', amount_cents=None, days_ahead=10):
    """Detecta se uma despesa avulsa corresponde a uma conta aberta próxima do vencimento (ou atrasada),
    evitando contagem dupla (ex.: Combustível avulso x ficha semanal de Combustível)."""
    today = now().date()
    limit_iso = (today + timedelta(days=days_ahead)).isoformat()

    def _norm(s):
        return re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode('ascii')).strip()

    text_norm = _norm(f'{category} {description}')
    text_words = {w for w in text_norm.split() if len(w) >= 4 and w not in ('conta', 'pagamento', 'para', 'carro', 'moto', 'mensal', 'plano')}
    if not text_words:
        return None

    best = None
    for d in get_debts(open_only=True):
        if d['due_date'] > limit_iso:
            continue
        title_norm = _norm(d['title'])
        title_words = {w for w in title_norm.split() if len(w) >= 4 and w not in ('conta', 'cartao', 'plano')}
        if not title_words:
            continue
        overlap = text_words & title_words
        if overlap or (title_norm and title_norm in text_norm):
            due_amt = monthly_due_amount(d)
            score = len(overlap) * 10 + (5 if amount_cents and abs(int(amount_cents) - due_amt) <= max(1500, int(due_amt * 0.5)) else 0)
            if best is None or score > best[0] or (score == best[0] and d['due_date'] < best[1]['due_date']):
                best = (score, d)
    return best[1] if best else None


def update_debt(debt_id, new_amount, new_due_date=None, new_title=None):
    """Atualiza o valor total ou vencimento de uma conta/dívida existente."""
    if new_amount <= 0:
        raise ValueError('O valor precisa ser positivo.')
    with conn() as c:
        d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d:
            raise ValueError(f'Dívida #{debt_id} não encontrada.')
        if new_amount < d['paid_amount']:
            raise ValueError(f"O valor não pode ser menor que o valor já pago ({brl(d['paid_amount'])}).")
        
        due = new_due_date or d['due_date']
        title = (new_title.strip()[:180] if new_title else d['title'])
        c.execute('UPDATE debts SET original_amount=?, due_date=?, title=? WHERE id=?',
                  (new_amount, due, title, debt_id))
        if due != d['due_date']:
            c.execute('DELETE FROM debt_notifications WHERE debt_id=?', (debt_id,))
        return new_amount - d['paid_amount']


def remove_debt(debt_id):
    """Remove uma conta/dívida cadastrada se não tiver pagamentos vinculados."""
    with conn() as c:
        payments = c.execute('SELECT COUNT(*) FROM debt_payments WHERE debt_id=?', (debt_id,)).fetchone()[0]
        if payments > 0:
            raise ValueError(f'Dívida #{debt_id} possui pagamentos vinculados no histórico. Não pode ser excluída.')
        c.execute('DELETE FROM debts WHERE id=?', (debt_id,))


def get_transaction(tx_id):
    with conn() as c:
        row = c.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    return dict(row) if row else None


def delete_transaction(tx_id):
    """Apaga um lançamento avulso. Lançamentos ligados a dívidas são protegidos."""
    with conn() as c:
        row = c.execute('SELECT debt_id FROM transactions WHERE id=?', (tx_id,)).fetchone()
        if not row:
            raise ValueError('Lançamento não encontrado.')
        if row['debt_id']:
            raise ValueError('Pagamento de dívida não pode ser apagado por aqui.')
        c.execute('DELETE FROM transactions WHERE id=?', (tx_id,))


def update_transaction(tx_id, amount=None, category=None, description=None, occurred_date=None):
    """Atualiza campos de um lançamento avulso. Pagamentos de dívida continuam protegidos."""
    with conn() as c:
        row = c.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
        if not row:
            raise ValueError('Lançamento não encontrado.')
        if row['debt_id']:
            raise ValueError('Pagamento de dívida não pode ser editado por aqui; estorne e lance novamente.')
        new_amt = int(amount) if amount is not None else row['amount']
        if new_amt <= 0:
            raise ValueError('O valor precisa ser positivo.')
        new_cat = category.strip()[:70] if category and category.strip() else row['category']
        new_desc = description.strip()[:250] if description is not None and description.strip() else row['description']
        new_stamp = row['occurred_at']
        if occurred_date:
            day = date.fromisoformat(str(occurred_date))
            _check_date_window(day)
            time_part = datetime.fromisoformat(row['occurred_at']).timetz().replace(tzinfo=None)
            new_stamp = datetime.combine(day, time_part, tzinfo=TZ).isoformat()
        c.execute(
            'UPDATE transactions SET amount=?, category=?, description=?, occurred_at=? WHERE id=?',
            (new_amt, new_cat, new_desc, new_stamp, tx_id),
        )
        return dict(c.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone())


def refund_debt_payment(tx_id):
    """Estorna um pagamento de dívida de forma atômica, restaurando o saldo da conta."""
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        tx = c.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
        if not tx:
            raise ValueError('Lançamento não encontrado.')
        debt_id = tx['debt_id']
        if not debt_id:
            raise ValueError('Este lançamento não é um pagamento de dívida.')
        amount = tx['amount']
        c.execute('DELETE FROM debt_payments WHERE transaction_id=?', (tx_id,))
        c.execute('UPDATE debts SET paid_amount=MAX(0, paid_amount-?) WHERE id=?', (amount, debt_id))
        c.execute('DELETE FROM transactions WHERE id=?', (tx_id,))
        d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        remaining = (d['original_amount'] - d['paid_amount']) if d else 0
        return {'debt_id': debt_id, 'title': d['title'] if d else tx['description'],
                'amount': amount, 'remaining': remaining}


def transaction_exists(kind, amount, category, description, day):
    """Usado pela importação para não duplicar lançamentos idênticos no mesmo dia."""
    with conn() as c:
        return c.execute('''SELECT 1 FROM transactions WHERE kind=? AND amount=? AND category=?
            AND description=? AND substr(occurred_at,1,10)=? LIMIT 1''',
            (kind, amount, category.strip()[:70], description.strip()[:250], day)).fetchone() is not None


def get_transactions(limit=15, offset=0, kind=None):
    clauses = []
    params = []
    if kind in ('receita', 'despesa'):
        clauses.append('kind=?')
        params.append(kind)
    sql = 'SELECT * FROM transactions'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY substr(occurred_at, 1, 10) DESC, id DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params)]


def get_transactions_by_date(day_iso, kind=None):
    """Retorna todas as movimentações de uma data específica (AAAA-MM-DD)."""
    clauses = ['substr(occurred_at, 1, 10) = ?']
    params = [str(day_iso)[:10]]
    if kind in ('receita', 'despesa'):
        clauses.append('kind=?')
        params.append(kind)
    sql = f"SELECT * FROM transactions WHERE {' AND '.join(clauses)} ORDER BY id ASC"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params)]


def month_spending_breakdown(month=None):
    """Separa todos os ganhos e gastos reais do mês (fixos/dívidas vs variáveis/avulsos)
    e calcula o ritmo real de gastos variáveis para um planejamento responsável."""
    current = now().strftime('%Y-%m')
    month = month or current
    year, month_num = (int(x) for x in month.split('-'))
    days_in_month = calendar.monthrange(year, month_num)[1]
    elapsed_days = max(1, now().day) if month == current else days_in_month
    remaining_days = max(0, days_in_month - now().day) if month == current else days_in_month
    cutoff_30d = (now().date() - timedelta(days=29)).isoformat()

    with conn() as c:
        txs = [dict(r) for r in c.execute(
            'SELECT kind, amount, debt_id, occurred_at FROM transactions WHERE substr(occurred_at,1,7)=?',
            (month,),
        )]
        recent_var = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(DISTINCT substr(occurred_at,1,10)) AS active_days "
            "FROM transactions WHERE kind='despesa' AND debt_id IS NULL AND substr(occurred_at,1,10) >= ?",
            (cutoff_30d,),
        ).fetchone()
        first_tx_row = c.execute("SELECT MIN(substr(occurred_at,1,10)) AS d FROM transactions").fetchone()

    income_total = sum(t['amount'] for t in txs if t['kind'] == 'receita')
    debt_paid_total = sum(t['amount'] for t in txs if t['kind'] == 'despesa' and t['debt_id'])
    variable_expense_total = sum(t['amount'] for t in txs if t['kind'] == 'despesa' and not t['debt_id'])
    expense_total = debt_paid_total + variable_expense_total

    # Ritmo diário baseado no histórico real disponível (máximo 30 dias)
    if first_tx_row and first_tx_row['d']:
        span_days = max(1, min(30, (now().date() - date.fromisoformat(first_tx_row['d'])).days + 1))
    else:
        span_days = elapsed_days
    recent_30d_var = int(recent_var['total']) if recent_var else 0
    daily_var_avg = round(variable_expense_total / elapsed_days) if variable_expense_total > 0 else round(recent_30d_var / span_days)
    monthly_var_pace = round(daily_var_avg * days_in_month)

    return {
        'month': month,
        'days_in_month': days_in_month,
        'elapsed_days': elapsed_days,
        'remaining_days': remaining_days,
        'income_total': income_total,
        'expense_total': expense_total,
        'debt_paid_total': debt_paid_total,
        'variable_expense_total': variable_expense_total,
        'recent_30d_variable_total': recent_30d_var,
        'daily_variable_avg': daily_var_avg,
        'monthly_variable_pace': monthly_var_pace,
    }


def summary(month=None):
    month = month or now().strftime('%Y-%m')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('Mês inválido. Use AAAA-MM.')
    with conn() as c:
        rows = c.execute('''SELECT kind, COALESCE(SUM(amount),0) total
             FROM transactions WHERE substr(occurred_at,1,7)=? GROUP BY kind''', (month,)).fetchall()
        totals = {r['kind']:r['total'] for r in rows}
        alltime = c.execute('''SELECT kind, COALESCE(SUM(amount),0) total
             FROM transactions GROUP BY kind''').fetchall()
        grand = {r['kind']:r['total'] for r in alltime}
        bycategory = [dict(r) for r in c.execute('''SELECT category, SUM(amount) total FROM transactions
                WHERE kind='despesa' AND substr(occurred_at,1,7)=?
                GROUP BY category ORDER BY total DESC LIMIT 8''',(month,))]
    return {'month':month, 'receita':totals.get('receita',0), 'despesa':totals.get('despesa',0),
            'total':grand.get('receita',0)-grand.get('despesa',0), 'categories':bycategory}


def next_month(month):
    d = date.fromisoformat(month + '-01')
    return (date(d.year+1, 1, 1) if d.month == 12 else date(d.year, d.month+1,1)).strftime('%Y-%m')


def get_fixed_incomes(active_only=True):
    sql = 'SELECT * FROM fixed_incomes'
    if active_only:
        sql += ' WHERE active=1'
    sql += ' ORDER BY day, id'
    with conn() as c:
        return [dict(r) for r in c.execute(sql)]


def add_fixed_income(title, amount, day, category='Renda Fixa'):
    if amount <= 0 or not str(title or '').strip():
        raise ValueError('Receita fixa inválida.')
    if not 1 <= int(day) <= 30:
        raise ValueError('Informe o dia do mês entre 1 e 30.')
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO preferences(key, value) VALUES('fixed_incomes_seeded', '1')")
        return c.execute(
            'INSERT INTO fixed_incomes(title, amount, day, category) VALUES(?, ?, ?, ?)',
            (title.strip()[:180], int(amount), int(day), (category or 'Renda Fixa').strip()[:70]),
        ).lastrowid


def update_fixed_income(inc_id, new_amount):
    if int(new_amount) <= 0:
        raise ValueError('Valor inválido.')
    with conn() as c:
        r = c.execute('SELECT * FROM fixed_incomes WHERE id=?', (inc_id,)).fetchone()
        if not r:
            raise ValueError(f'Receita fixa #{inc_id} não encontrada.')
        c.execute('UPDATE fixed_incomes SET amount=? WHERE id=?', (int(new_amount), inc_id))
        return dict(r, amount=int(new_amount))


def remove_fixed_income(inc_id):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO preferences(key, value) VALUES('fixed_incomes_seeded', '1')")
        if c.execute('DELETE FROM fixed_incomes WHERE id=?', (inc_id,)).rowcount == 0:
            raise ValueError(f'Receita fixa #{inc_id} não encontrada.')


def fixed_income_status(month):
    """Verifica quais receitas fixas cadastradas já entraram em `month` e quanto ainda falta receber,
    sem abater receitas extras avulsas e sem confundir parcelas diferentes do salário."""
    incomes = get_fixed_incomes(active_only=True)
    expected = sum(i['amount'] for i in incomes)
    current = now().strftime('%Y-%m')

    if month < current:
        txs_past = [t for t in month_transactions(month) if t['kind'] == 'receita']
        total_past = sum(int(t['amount']) for t in txs_past)
        items = [dict(i, received=True) for i in incomes]
        return {
            'expected': expected, 'received_fixed': expected, 'remaining_fixed': 0,
            'received_extra': max(0, total_past - expected), 'received_total': total_past,
            'items': items,
        }
    if month > current:
        items = [dict(i, received=False) for i in incomes]
        return {
            'expected': expected, 'received_fixed': 0, 'remaining_fixed': expected,
            'received_extra': 0, 'received_total': 0, 'items': items,
        }

    txs = [t for t in month_transactions(month) if t['kind'] == 'receita']
    used_tx_ids = set()
    matched_map = {}

    def _norm(s):
        return unicodedata.normalize('NFKD', str(s or '').lower()).encode('ascii', 'ignore').decode('ascii')

    # Passada 1: valor exato (com preferência se o título também combinar)
    for inc in incomes:
        inc_norm = _norm(inc['title'])
        candidates = [t for t in txs if t['id'] not in used_tx_ids and int(t['amount']) == inc['amount']]
        if candidates:
            chosen = next(
                (t for t in candidates if any(w in _norm(f"{t['category']} {t['description']}") for w in re.findall(r'[a-z0-9]{3,}', inc_norm))),
                candidates[0],
            )
            matched_map[inc['id']] = chosen
            used_tx_ids.add(chosen['id'])

    # Passada 2: valor próximo (<= 8% e <= R$ 50) + compatibilidade de título/dia (nunca confunde dia 15 com dia 30)
    for inc in incomes:
        if inc['id'] in matched_map:
            continue
        inc_norm = _norm(inc['title'])
        inc_words = [w for w in re.findall(r'[a-z0-9]+', inc_norm) if len(w) >= 4 and w not in ('parcela', 'salario', 'renda', 'fixa')]
        tol = min(5000, max(1000, int(inc['amount'] * 0.08)))
        for t in txs:
            if t['id'] in used_tx_ids:
                continue
            t_amt = int(t['amount'])
            if abs(t_amt - inc['amount']) > tol:
                continue
            t_norm = _norm(f"{t['category']} {t['description']}")
            day_ok = True
            if '15' in inc_norm and ('30' in t_norm or 'fim' in t_norm or 'complemento' in t_norm):
                day_ok = False
            if ('30' in inc_norm or 'fim' in inc_norm) and '15' in t_norm:
                day_ok = False
            word_ok = bool(inc_words and any(w in t_norm for w in inc_words)) or ('sal' in inc_norm and 'sal' in t_norm)
            if day_ok and word_ok:
                matched_map[inc['id']] = t
                used_tx_ids.add(t['id'])
                break

    items = []
    received_fixed = 0
    for inc in incomes:
        is_rec = inc['id'] in matched_map
        if is_rec:
            received_fixed += inc['amount']
        items.append(dict(inc, received=is_rec))

    received_total = sum(int(t['amount']) for t in txs)
    received_extra = sum(int(t['amount']) for t in txs if t['id'] not in used_tx_ids)

    return {
        'expected': expected,
        'received_fixed': received_fixed,
        'remaining_fixed': max(0, expected - received_fixed),
        'received_extra': received_extra,
        'received_total': received_total,
        'items': items,
    }


def _months_diff(m_start, m_end):
    y1, m1 = (int(x) for x in m_start.split('-'))
    y2, m2 = (int(x) for x in m_end.split('-'))
    return (y2 - y1) * 12 + (m2 - m1)


def project_month_debts(target_m):
    """Retorna todas as contas previstas para `target_m`:
    - Parcelas/contas já cadastradas em `debts` para `target_m` (projected=False)
    - Se `target_m` for mês futuro onde as contas fixas (`recurring_bills`) e os saldos
      rolantes de R$ 100/mês (`Perfumes` e `Roupas`) ainda não foram gerados/avançados,
      inclui sua projeção exata (projected=True) para não subestimar os gastos dos meses seguintes."""
    current = now().strftime('%Y-%m')
    open_debts = get_debts(open_only=True)
    debt_months = get_debt_months()
    first_active_m = current if any(d['due_date'][:7] <= current for d in open_debts) else (debt_months[0] if debt_months else current)

    if target_m == current:
        actual = [d for d in open_debts if d['due_date'][:7] <= target_m]
    else:
        actual = [d for d in open_debts if d['due_date'][:7] == target_m]

    items = []
    for d in actual:
        items.append({
            'id': d['id'],
            'title': d['title'],
            'due_date': d['due_date'],
            'monthly_due': monthly_due_amount(d),
            'remaining': d['original_amount'] - d['paid_amount'],
            'projected': False,
        })

    if target_m > first_active_m:
        year, month_num = (int(x) for x in target_m.split('-'))
        max_day = calendar.monthrange(year, month_num)[1]
        # 1) Contas fixas ativas ainda não geradas em `target_m`
        for r in get_recurring(active_only=True):
            if r.get('last_generated') and r['last_generated'] >= target_m:
                continue
            if r.get('weekday') is not None:
                for d_num in range(1, max_day + 1):
                    dt = date(year, month_num, d_num)
                    if dt.weekday() == r['weekday']:
                        due_iso = dt.isoformat()
                        if not any(x['title'] == r['title'] and x['due_date'] == due_iso for x in items):
                            items.append({
                                'id': None,
                                'title': r['title'],
                                'due_date': due_iso,
                                'monthly_due': r['amount'],
                                'remaining': r['amount'],
                                'projected': True,
                            })
            else:
                due_iso = f"{target_m}-{min(int(r['day']), max_day):02d}"
                if not any(x['title'] == r['title'] for x in items):
                    items.append({
                        'id': None,
                        'title': r['title'],
                        'due_date': due_iso,
                        'monthly_due': r['amount'],
                        'remaining': r['amount'],
                        'projected': True,
                    })

        # 2) Contas com teto mensal configurável (ex.: Perfumes, Roupas ou qualquer conta com monthly_cap) em mês anterior
        for d in open_debts:
            d_m = d['due_date'][:7]
            rem = d['original_amount'] - d['paid_amount']
            m_amt = monthly_due_amount(d)
            if m_amt < rem and d_m < target_m:
                k = _months_diff(d_m, target_m)
                rem_at_target = rem - k * m_amt
                if rem_at_target > 0 and not any(x['title'] == d['title'] for x in items):
                    amt_target = min(m_amt, rem_at_target)
                    day_orig = int(d['due_date'][8:10])
                    due_iso = f"{target_m}-{min(day_orig, max_day):02d}"
                    items.append({
                        'id': d['id'],
                        'title': d['title'],
                        'due_date': due_iso,
                        'monthly_due': amt_target,
                        'remaining': rem_at_target,
                        'projected': True,
                    })

    items.sort(key=lambda x: (x['due_date'], x['title']))
    return items


def add_expected_income(title, amount, expected_date, category='Renda Extra'):
    """Cadastra uma receita futura pontual (ex.: 13º salário, freela fechado, reembolso)."""
    if int(amount) <= 0 or not str(title or '').strip():
        raise ValueError('Receita futura inválida.')
    day = date.fromisoformat(str(expected_date))
    _check_date_window(day)
    with conn() as c:
        return c.execute(
            'INSERT INTO expected_incomes(title, amount, expected_date, category) VALUES(?, ?, ?, ?)',
            (title.strip()[:180], int(amount), day.isoformat(), (category or 'Renda Extra').strip()[:70]),
        ).lastrowid


def get_expected_incomes(pending_only=True, month=None):
    clauses = []
    params = []
    if pending_only:
        clauses.append('received_tx_id IS NULL')
    if month:
        clauses.append('substr(expected_date, 1, 7) = ?')
        params.append(month)
    sql = 'SELECT * FROM expected_incomes'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY expected_date, id'
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params)]


def receive_expected_income(exp_id, occurred_date=None):
    """Confirma o recebimento de uma receita agendada: cria a transação e marca como recebida atomicamente."""
    stamp = now().isoformat()
    if occurred_date:
        day = date.fromisoformat(str(occurred_date))
        _check_date_window(day)
        current = now()
        stamp = datetime.combine(day, current.timetz().replace(tzinfo=None), tzinfo=TZ).isoformat()
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM expected_incomes WHERE id=? AND received_tx_id IS NULL', (exp_id,)).fetchone()
        if not row:
            raise ValueError(f'Receita a receber #{exp_id} não encontrada ou já recebida.')
        tx_id = c.execute(
            'INSERT INTO transactions(kind, amount, category, description, occurred_at) VALUES(?,?,?,?,?)',
            ('receita', row['amount'], row['category'], row['title'], stamp),
        ).lastrowid
        c.execute('UPDATE expected_incomes SET received_tx_id=? WHERE id=?', (tx_id, exp_id))
        return {'tx_id': tx_id, 'title': row['title'], 'amount': row['amount'], 'category': row['category']}


def remove_expected_income(exp_id):
    with conn() as c:
        if c.execute('DELETE FROM expected_incomes WHERE id=? AND received_tx_id IS NULL', (exp_id,)).rowcount == 0:
            raise ValueError(f'Receita a receber #{exp_id} não encontrada.')


def get_variable_budget():
    raw = get_pref('variable_monthly_budget', '0')
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return 0


def set_variable_budget(cents):
    val = max(0, int(cents))
    set_pref('variable_monthly_budget', str(val))
    return val


def _simulate_month_cashflow(month, opening_balance, items, inc_info, extra_incomes=None, extra_outflows=None):
    """Simula o caixa dia a dia dentro de `month` para detectar qualquer vale/negativo antes de receitas
    (ex.: contas de 01 a 14 vencendo antes do salário do dia 15)."""
    current = now().strftime('%Y-%m')
    today_iso = now().date().isoformat()
    year, month_num = (int(x) for x in month.split('-'))
    max_day = calendar.monthrange(year, month_num)[1]

    by_day = {}

    def _add(ev_date, delta, kind, label):
        entry = by_day.setdefault(ev_date, {'in': 0, 'out': 0, 'events': []})
        if kind == 'in':
            entry['in'] += delta
        else:
            entry['out'] += delta
        entry['events'].append((kind, delta, label))

    for x in items:
        eff_date = x['due_date']
        if month == current and eff_date < today_iso:
            eff_date = today_iso
        _add(eff_date, x['monthly_due'], 'out', x['title'])

    for inc in inc_info.get('items', []):
        if inc.get('received'):
            continue
        eff_date = f"{month}-{min(int(inc['day']), max_day):02d}"
        if month == current and eff_date < today_iso:
            eff_date = today_iso
        _add(eff_date, inc['amount'], 'in', inc['title'])

    for exp in (extra_incomes or []):
        eff_date = exp['expected_date']
        if month == current and eff_date < today_iso:
            eff_date = today_iso
        _add(eff_date, exp['amount'], 'in', exp['title'])

    for out_dt, out_amt, out_lbl in (extra_outflows or []):
        if out_amt <= 0:
            continue
        eff_date = out_dt
        if month == current and eff_date < today_iso:
            eff_date = today_iso
        _add(eff_date, out_amt, 'out', out_lbl)

    start_date = today_iso if month == current else f'{month}-01'
    bal = opening_balance
    min_balance = opening_balance
    min_balance_date = start_date
    day14_iso = f'{month}-14'
    day15_iso = f'{month}-15'
    before_day15_balance = opening_balance
    end_q1_balance = opening_balance
    timeline = []

    for d_iso in sorted(by_day):
        ev = by_day[d_iso]
        bal = bal + ev['in'] - ev['out']
        if bal < min_balance:
            min_balance = bal
            min_balance_date = d_iso
        if d_iso <= day14_iso:
            before_day15_balance = bal
        if d_iso <= day15_iso:
            end_q1_balance = bal
        timeline.append({'date': d_iso, 'in': ev['in'], 'out': ev['out'], 'balance': bal, 'events': ev['events']})

    if not any(t['date'] <= day15_iso for t in timeline):
        end_q1_balance = before_day15_balance

    negative_days = [(t['date'], t['balance']) for t in timeline if t['balance'] < 0]

    return {
        'before_day15_balance': before_day15_balance,
        'end_q1_balance': end_q1_balance,
        'min_balance': min_balance,
        'min_balance_date': min_balance_date,
        'shortage': abs(min_balance) if min_balance < 0 else 0,
        'negative_days': negative_days,
        'closing_balance': bal,
        'timeline': timeline,
    }


def _budget_weekly_outflows(month, remaining_budget_cents):
    """Distribui o orçamento mensal de gastos variáveis em blocos semanais dentro de `month`."""
    if remaining_budget_cents <= 0:
        return []
    current = now().strftime('%Y-%m')
    today_day = now().day
    year, month_num = (int(x) for x in month.split('-'))
    max_day = calendar.monthrange(year, month_num)[1]
    anchors = [d for d in (7, 14, 21, min(28, max_day)) if month != current or d >= today_day]
    if not anchors:
        anchors = [min(max_day, today_day)]
    base = remaining_budget_cents // len(anchors)
    rem = remaining_budget_cents % len(anchors)
    return [
        (f'{month}-{d:02d}', base + (1 if i < rem else 0), 'Orçamento gastos avulsos')
        for i, d in enumerate(anchors)
        if base + (1 if i < rem else 0) > 0
    ]


def cumulative_planning(target_m=None, extra_outflows_by_month=None):
    """Calcula o fluxo de caixa cronológico em cascata desde o 1º mês ativo até `target_m`
    (incluindo receitas fixas, receitas pontuais agendadas, teto mensal de contas e orçamento de avulsos)."""
    current = now().strftime('%Y-%m')
    s = summary(current)
    open_debts = get_debts(open_only=True)
    debt_months = get_debt_months()
    all_expected = get_expected_incomes(pending_only=True)
    cur_inc = fixed_income_status(current)
    has_current_pending = (
        any(d['due_date'][:7] <= current for d in open_debts)
        or cur_inc['remaining_fixed'] > 0
        or any(e['expected_date'][:7] <= current for e in all_expected)
        or bool((extra_outflows_by_month or {}).get(current))
    )
    start_m = current if has_current_pending else (debt_months[0] if debt_months else current)
    target_m = target_m or start_m

    lookahead_m = next_month(target_m) if target_m >= start_m else target_m
    seq = [start_m]
    cur_iter = start_m
    while cur_iter < lookahead_m and len(seq) < 36:
        cur_iter = next_month(cur_iter)
        seq.append(cur_iter)
    if target_m < start_m:
        seq = [target_m]

    running_balance = s['total']
    running_prudent = s['total']
    var_budget = get_variable_budget()
    breakdown_current = month_spending_breakdown(current)
    step_data = {}

    for m in seq:
        opening = running_balance
        opening_prudent = running_prudent
        inc_info = fixed_income_status(m)
        items = project_month_debts(m)
        if m == current:
            exp_items = [e for e in all_expected if e['expected_date'][:7] <= m]
        else:
            exp_items = [e for e in all_expected if e['expected_date'][:7] == m]
        exp_total = sum(e['amount'] for e in exp_items)

        q1_items = [x for x in items if int(x['due_date'][8:10]) <= 15]
        q2_items = [x for x in items if int(x['due_date'][8:10]) > 15]
        q1_total = sum(x['monthly_due'] for x in q1_items)
        q2_total = sum(x['monthly_due'] for x in q2_items)
        registered_total = sum(x['monthly_due'] for x in items if not x['projected'])
        projected_fixed_total = sum(x['monthly_due'] for x in items if x['projected'])
        flow_total = q1_total + q2_total

        sim_extra_outs = list((extra_outflows_by_month or {}).get(m, []))
        sim = _simulate_month_cashflow(m, opening, items, inc_info, extra_incomes=exp_items, extra_outflows=sim_extra_outs)
        closing = sim['closing_balance']

        # Cenário prudente considerando o orçamento de gastos avulsos configurado pelo usuário
        prudent_sim = None
        var_rem_m = 0
        if var_budget > 0:
            var_rem_m = max(0, var_budget - breakdown_current['variable_expense_total']) if m == current else var_budget
            budget_outs = sim_extra_outs + _budget_weekly_outflows(m, var_rem_m)
            prudent_sim = _simulate_month_cashflow(m, opening_prudent, items, inc_info, extra_incomes=exp_items, extra_outflows=budget_outs)
            running_prudent = prudent_sim['closing_balance']
        else:
            running_prudent = closing

        step_data[m] = {
            'month': m,
            'start_m': start_m,
            'opening_balance': opening,
            'income_info': inc_info,
            'expected_extra_items': exp_items,
            'expected_extra_total': exp_total,
            'items': items,
            'q1_items': q1_items,
            'q2_items': q2_items,
            'q1_total': q1_total,
            'q2_total': q2_total,
            'registered_total': registered_total,
            'projected_fixed_total': projected_fixed_total,
            'flow_total': flow_total,
            'closing_balance': closing,
            'before_day15_balance': sim['before_day15_balance'],
            'end_q1_balance': sim['end_q1_balance'],
            'min_balance': sim['min_balance'],
            'min_balance_date': sim['min_balance_date'],
            'shortage': sim['shortage'],
            'negative_days': sim['negative_days'],
            'timeline': sim['timeline'],
            'breakdown': breakdown_current,
            'variable_budget': var_budget,
            'variable_budget_remaining': var_rem_m,
            'prudent_min_balance': prudent_sim['min_balance'] if prudent_sim else None,
            'prudent_min_date': prudent_sim['min_balance_date'] if prudent_sim else None,
            'prudent_closing_balance': prudent_sim['closing_balance'] if prudent_sim else None,
            'prudent_shortage': prudent_sim['shortage'] if prudent_sim else 0,
        }
        running_balance = closing

    res = step_data[target_m]
    nxt = step_data.get(next_month(target_m))
    if nxt:
        res['next_month'] = nxt['month']
        res['next_month_min_balance'] = nxt['min_balance']
        res['next_month_min_date'] = nxt['min_balance_date']
        res['next_month_shortage'] = nxt['shortage']
        res['next_month_q1_total'] = nxt['q1_total']
        res['next_month_before_day15'] = nxt['before_day15_balance']
    else:
        res['next_month'] = None
        res['next_month_min_balance'] = None
        res['next_month_min_date'] = None
        res['next_month_shortage'] = 0
        res['next_month_q1_total'] = 0
        res['next_month_before_day15'] = None
    return res


def simulate_purchase(total_cents, installments=1, first_due_iso=None):
    """Simula sem alterar o banco o impacto cronológico de uma compra à vista ou parcelada
    nos próximos 4 meses (detectando se algum dia fica negativo)."""
    if int(total_cents) <= 0:
        raise ValueError('Valor inválido para simulação.')
    count = max(1, min(48, int(installments or 1)))
    first_dt = first_due_iso or now().date().isoformat()
    date.fromisoformat(first_dt)

    base_val = int(total_cents) // count
    rem = int(total_cents) % count
    extra_by_m = {}
    cur_dt = first_dt
    parts = []
    for idx in range(1, count + 1):
        amt = base_val + (1 if idx <= rem else 0)
        lbl = f'Simulação [{idx}/{count}]' if count > 1 else 'Simulação à vista'
        extra_by_m.setdefault(cur_dt[:7], []).append((cur_dt, amt, lbl))
        parts.append((cur_dt, amt))
        cur_dt = _next_month_due(cur_dt)

    current = now().strftime('%Y-%m')
    months_to_check = [current]
    it = current
    for _ in range(3):
        it = next_month(it)
        months_to_check.append(it)
    for m_k in sorted(extra_by_m):
        if m_k not in months_to_check and len(months_to_check) < 6:
            months_to_check.append(m_k)
    months_to_check = sorted(set(months_to_check))[:4]

    report_months = []
    any_negative = False
    worsens_shortage = False
    for m in months_to_check:
        before = cumulative_planning(m)
        after = cumulative_planning(m, extra_outflows_by_month=extra_by_m)
        if after['shortage'] > 0:
            any_negative = True
        if after['shortage'] > before['shortage']:
            worsens_shortage = True
        report_months.append({
            'month': m,
            'before_min': before['min_balance'],
            'after_min': after['min_balance'],
            'after_min_date': after['min_balance_date'],
            'before_closing': before['closing_balance'],
            'after_closing': after['closing_balance'],
            'before_shortage': before['shortage'],
            'after_shortage': after['shortage'],
        })

    return {
        'total_cents': int(total_cents),
        'installments': count,
        'parts': parts,
        'safe': not any_negative,
        'worsens_shortage': worsens_shortage,
        'months': report_months,
    }


def planning():
    current = now().strftime('%Y-%m')
    s = summary(current)
    next_m = next_month(current)
    after_m = next_month(next_m)
    open_debts = get_debts(open_only=True)
    due = sum(monthly_due_amount(d) for d in open_debts if d['due_date'] < next_m + '-01')
    following = sum(monthly_due_amount(d) for d in open_debts if next_m + '-01' <= d['due_date'] < after_m + '-01')
    return {'month':current,'balance':s['total'], 'income_month':s['receita'], 'spent_month':s['despesa'],
            'due_remaining_month':due,'due_next_month':following}


def insert_reminder(due_iso, content):
    if not content.strip(): raise ValueError('Descrição obrigatória.')
    with conn() as c:
        return c.execute('INSERT INTO reminders(due_at,content) VALUES(?,?)',
                         (due_iso,content.strip()[:300])).lastrowid


def due_reminders():
    """Lembretes vencidos. Compara datas reais (não texto) para não depender do formato salvo."""
    current = now()
    with conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM reminders WHERE sent_at IS NULL ORDER BY due_at')]
    due = []
    for r in rows:
        try:
            if datetime.fromisoformat(r['due_at']) <= current:
                due.append(r)
        except ValueError:
            continue
    return due[:20]


def snooze_reminder(rid, due_iso):
    """Reagenda um lembrete (volta a ficar pendente). Retorna True se existir."""
    with conn() as c:
        return c.execute('UPDATE reminders SET due_at=?, sent_at=NULL WHERE id=?', (due_iso, rid)).rowcount > 0


def mark_reminder(rid):
    with conn() as c:
        c.execute('UPDATE reminders SET sent_at=? WHERE id=? AND sent_at IS NULL',(now().isoformat(),rid))


def get_reminders():
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM reminders WHERE sent_at IS NULL ORDER BY due_at LIMIT 15')]


def get_reminder(rid):
    with conn() as c:
        row = c.execute('SELECT * FROM reminders WHERE id=? AND sent_at IS NULL', (rid,)).fetchone()
    return dict(row) if row else None


def delete_reminder(rid):
    with conn() as c:
        if c.execute('DELETE FROM reminders WHERE id=? AND sent_at IS NULL', (rid,)).rowcount == 0:
            raise ValueError(f'Lembrete pendente #{rid} não encontrado.')


def set_mode(mode):
    if mode not in ('auto','rapido','avancado','gemini'): raise ValueError('Modo inválido')
    with conn() as c:
        c.execute("INSERT INTO preferences(key,value) VALUES('model_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mode,))


def get_mode():
    with conn() as c:
        row = c.execute("SELECT value FROM preferences WHERE key='model_mode'").fetchone()
    return row['value'] if row else 'auto'


def remember(role, content):
    with conn() as c:
        c.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)',
                  (role, content[:3000],now().isoformat()))
        # Retenção: mantém só as 500 mensagens mais recentes (a API usa apenas as últimas 6).
        c.execute('DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - 500')


def last_messages(n=8, max_age_minutes=120):
    """Retorna as últimas `n` mensagens recentes (padrão: últimas 2h) para evitar mistura de assuntos antigos."""
    with conn() as c:
        rows = list(c.execute('SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT ?', (n,)))
    if max_age_minutes is not None:
        cutoff = now() - timedelta(minutes=max_age_minutes)
        filtered = []
        for row in rows:
            try:
                if datetime.fromisoformat(row['created_at']) >= cutoff:
                    filtered.append({'role': row['role'], 'content': row['content']})
            except ValueError:
                continue
        return list(reversed(filtered))
    return [{'role': row['role'], 'content': row['content']} for row in reversed(rows)]


def clear_messages():
    """Limpa o histórico de conversa da IA sem tocar nos dados financeiros."""
    with conn() as c:
        c.execute('DELETE FROM messages')


def set_profile(profile_dict):
    """Salva metas e diretrizes de renda/planejamento do usuário em preferences."""
    with conn() as c:
        for k, v in profile_dict.items():
            if v is not None and str(v).strip():
                c.execute(
                    "INSERT INTO preferences(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f'profile_{k}', str(v).strip())
                )


def get_profile():
    """Retorna metas e diretrizes salvas do usuário."""
    with conn() as c:
        rows = c.execute("SELECT key, value FROM preferences WHERE key LIKE 'profile_%'").fetchall()
    return {r['key'][8:]: r['value'] for r in rows}


def get_debts_due_soon():
    """Retorna dívidas abertas com vencimento em 3d, 1d, hoje ou em atraso."""
    today = now().date()
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM debts WHERE original_amount > paid_amount ORDER BY due_date'
        )]
    result = []
    for d in rows:
        due = date.fromisoformat(d['due_date'])
        days_left = (due - today).days
        if days_left == 3 or (days_left == 2 and d['created_at'][:10] < today.isoformat()):
            alert_type = '3d'
        elif days_left == 1:
            alert_type = '1d'
        elif days_left == 0:
            alert_type = 'today'
        elif days_left < 0:
            alert_type = 'overdue'
        else:
            continue
        result.append({**d, 'days_left': days_left, 'alert_type': alert_type,
                        'remaining': monthly_due_amount(d)})
    return result


def was_debt_notified(debt_id, alert_type):
    with conn() as c:
        row = c.execute('SELECT id FROM debt_notifications WHERE debt_id=? AND alert_type=?',
                        (debt_id, alert_type)).fetchone()
    return row is not None


def mark_debt_notified(debt_id, alert_type):
    with conn() as c:
        c.execute('INSERT OR IGNORE INTO debt_notifications(debt_id, alert_type, sent_at) VALUES(?,?,?)',
                  (debt_id, alert_type, now().isoformat()))


def get_pref(key, default=None):
    with conn() as c:
        row = c.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_pref(key, value):
    with conn() as c:
        c.execute("INSERT INTO preferences(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def previous_month(day=None):
    """Mês anterior (AAAA-MM) ao dia informado (padrão: hoje)."""
    day = day or now().date()
    return (day.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


def month_transactions(month):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('Mês inválido. Use AAAA-MM.')
    with conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT id, kind, amount, category, description, occurred_at FROM transactions '
            'WHERE substr(occurred_at,1,7)=? ORDER BY id', (month,))]


def morning_data(days=7):
    """Dados do resumo diário: saldo do mês, contas até `days` dias (e atrasadas) e lembretes de hoje."""
    today = now().date()
    limit = (today + timedelta(days=days)).isoformat()
    debts = [dict(d, remaining=monthly_due_amount(d),
                  days_left=(date.fromisoformat(d['due_date']) - today).days)
             for d in get_debts() if d['due_date'] <= limit]
    reminders = [r for r in get_reminders()
                 if datetime.fromisoformat(r['due_at']).astimezone(TZ).date() == today]
    return {'summary': summary(), 'debts': debts, 'reminders': reminders}


def ai_context():
    s = summary()
    debts = get_debts(open_only=True)
    tx = get_transactions(50)
    bd = month_spending_breakdown(s['month'])
    fixed_incomes = get_fixed_incomes(active_only=True)
    with conn() as c:
        last_inc = c.execute("SELECT * FROM transactions WHERE kind='receita' ORDER BY substr(occurred_at,1,10) DESC, id DESC LIMIT 1").fetchone()
        last_exp = c.execute("SELECT * FROM transactions WHERE kind='despesa' ORDER BY substr(occurred_at,1,10) DESC, id DESC LIMIT 1").fetchone()
    profile = get_profile()
    lines = [
        f"Mês atual: {s['month']}",
        f"Receitas registradas no mês: {brl(s['receita'])}",
        f"Despesas registradas no mês: {brl(s['despesa'])} (Contas/dívidas pagas: {brl(bd['debt_paid_total'])} | Gastos avulsos/variáveis: {brl(bd['variable_expense_total'])})",
        f"Ritmo médio atual de gastos avulsos/variáveis: {brl(bd['daily_variable_avg'])}/dia (~{brl(bd['monthly_variable_pace'])}/mês)",
        f"Saldo histórico registrado (caixa real de hoje): {brl(s['total'])}",
        ("Última receita real registrada no banco: "
         f"#{last_inc['id']} {short_date(last_inc['occurred_at'])} | {brl(last_inc['amount'])} | {last_inc['category']} — {last_inc['description']}"
         if last_inc else "Última receita real registrada no banco: Nenhuma"),
        ("Última despesa real registrada no banco: "
         f"#{last_exp['id']} {short_date(last_exp['occurred_at'])} | {brl(last_exp['amount'])} | {last_exp['category']} — {last_exp['description']}"
         if last_exp else "Última despesa real registrada no banco: Nenhuma"),
    ]

    if fixed_incomes:
        lines.append('')
        lines.append('Receitas fixas cadastradas no app:')
        for inc in fixed_incomes:
            lines.append(f"- Dia {inc['day']:02d}: {inc['title']} — {brl(inc['amount'])} ({inc['category']})")
        lines.append(f"  Total de receitas fixas mensais: {brl(sum(i['amount'] for i in fixed_incomes))}")

    pending_exp = get_expected_incomes(pending_only=True)
    if pending_exp:
        lines.append('')
        lines.append('Receitas futuras pontuais a receber:')
        for e in pending_exp:
            lines.append(f"- #{e['id']} {e['title']}: {brl(e['amount'])} (previsto {short_date(e['expected_date'])})")

    var_budget = get_variable_budget()
    if var_budget > 0:
        lines.append(f"Orçamento mensal definido para gastos avulsos/variáveis: {brl(var_budget)}/mês")

    # Perfil estruturado — metas e observações adicionais
    if profile:
        lines.append('')
        lines.append('=== PERFIL FINANCEIRO DO USUÁRIO (DADOS CONFIRMADOS) ===')
        if 'renda_fixa' in profile:
            lines.append('RENDA FIXA MENSAL (PERFIL):')
            lines.append(profile['renda_fixa'])
        elif 'renda_mensal_esperada' in profile:
            lines.append(f"Renda mensal total esperada: R$ {profile['renda_mensal_esperada']}")
        if 'renda_unica' in profile:
            lines.append('RECEITAS ÚNICAS (NÃO RECORRENTES):')
            lines.append(profile['renda_unica'])
        if 'previsao_meses' in profile:
            lines.append('PREVISÃO DE RECEITAS POR MÊS:')
            lines.append(profile['previsao_meses'])
        if 'meta_poupanca_mensal' in profile:
            lines.append(f"Meta de poupança mensal: R$ {profile['meta_poupanca_mensal']}")
        if 'observacoes_planejamento' in profile:
            lines.append(f"Observações: {profile['observacoes_planejamento']}")
        lines.append('=== FIM DO PERFIL ===')

    recurring = get_recurring()
    if recurring:
        w_names = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo')
        lines.append('')
        lines.append('Contas fixas cadastradas:')
        for r in recurring:
            when_str = f"toda {w_names[r['weekday']]}" if r.get('weekday') is not None else f"todo dia {r['day']:02d}"
            lines.append(f"- #{r['id']} {r['title']}: {brl(r['amount'])} ({when_str})")

    with conn() as c:
        paid_month_debts = [dict(r) for r in c.execute(
            '''SELECT d.id, d.title, d.due_date, d.original_amount, d.paid_amount,
                      MAX(dp.paid_at) AS paid_at
               FROM debts d
               LEFT JOIN debt_payments dp ON dp.debt_id = d.id
               WHERE d.paid_amount >= d.original_amount
                 AND (substr(d.due_date, 1, 7) = ? OR substr(dp.paid_at, 1, 7) = ?)
               GROUP BY d.id
               ORDER BY d.due_date, d.id''',
            (s['month'], s['month']),
        )]

    if paid_month_debts:
        lines.append('')
        lines.append(f"Contas/dívidas JÁ QUITADAS referentes a {s['month'][5:]}/{s['month'][:4]} (NÃO estão atrasadas):")
        for pd in paid_month_debts:
            paid_info = f"paga em {short_date(pd['paid_at'])}" if pd.get('paid_at') else 'quitada'
            lines.append(f"- #{pd['id']} {pd['title']}: {brl(pd['original_amount'])} (vencia {short_date(pd['due_date'])}, {paid_info})")

    lines.append('')
    lines.append('Dívidas/contas abertas (organizadas por mês de vencimento):')
    if debts:
        cur_m = None
        m_sub = 0
        for d in debts:
            m_due = d['due_date'][:7]
            if m_due != cur_m:
                if cur_m is not None:
                    lines.append(f"  => Subtotal a pagar nas fichas de {cur_m[5:]}/{cur_m[:4]}: {brl(m_sub)}")
                cur_m = m_due
                m_sub = 0
                lines.append(f"  [Vencimento em {m_due[5:]}/{m_due[:4]}]:")
            rem = d['original_amount'] - d['paid_amount']
            m_due_amt = monthly_due_amount(d)
            m_sub += m_due_amt
            if m_due_amt < rem:
                lines.append(f"  - #{d['id']} {d['title']}: a pagar neste mês {brl(m_due_amt)} (saldo total restante: {brl(rem)}); vence {short_date(d['due_date'])}")
            else:
                lines.append(f"  - #{d['id']} {d['title']}: falta {brl(rem)}; vence {short_date(d['due_date'])}")
        if cur_m is not None:
            lines.append(f"  => Subtotal a pagar nas fichas de {cur_m[5:]}/{cur_m[:4]}: {brl(m_sub)}")
        lines.append('')
        lines.append('=== PROJEÇÃO CRONOLÓGICA OFICIAL POR MÊS (USE ESTES NÚMEROS EXATOS) ===')
        proj_months = sorted({s['month']} | set(get_debt_months()[:4]))[:4]
        for m_proj in proj_months:
            cp = cumulative_planning(m_proj)
            m_lbl = f"{m_proj[5:]}/{m_proj[:4]}"
            if cp['shortage'] > 0:
                neg_list = ', '.join(f"{short_date(dt)[:5]} ({brl(b)})" for dt, b in cp['negative_days'][:4])
                risk_note = f" | ALERTA CAIXA NEGATIVO NO MEIO DO MÊS: {neg_list} (pior ponto: {brl(cp['min_balance'])} em {short_date(cp['min_balance_date'])})"
            else:
                risk_note = f" | Menor saldo no mês: {brl(cp['min_balance'])} em {short_date(cp['min_balance_date'])}"
            lines.append(
                f"- Mês {m_lbl}: Saldo inicial {brl(cp['opening_balance'])} + "
                f"Receitas fixas a entrar {brl(cp['income_info']['remaining_fixed'])} - "
                f"Contas do mês {brl(cp['flow_total'])} "
                f"(fichas: {brl(cp['registered_total'])}, fixas/recorrentes previstas: {brl(cp['projected_fixed_total'])}) "
                f"= Saldo final comprometido: {brl(cp['closing_balance'])} "
                f"| Antes do dia 15: {brl(cp['before_day15_balance'])}{risk_note}"
            )
    else:
        lines.append('- Nenhuma cadastrada')
    lines.append('')
    lines.append('Movimentações recentes (mais novas primeiro):')
    lines += [f"- #{t['id']} {short_date(t['occurred_at'])} | {t['kind']}: {brl(t['amount'])}, {t['category']}, {t['description'][:60]}" for t in tx] or ['- Nenhuma cadastrada']
    return '\n'.join(lines)



def add_recurring(title, amount, day=0, weekday=None, last_generated=False):
    if amount <= 0 or not title.strip():
        raise ValueError('Conta fixa inválida.')
    if weekday is not None:
        if not 0 <= int(weekday) <= 6:
            raise ValueError('Dia da semana inválido (0=segunda a 6=domingo).')
        day_val = 0
        wk_val = int(weekday)
        last = last_generated if isinstance(last_generated, str) else None
    else:
        if not 1 <= int(day) <= 30:
            raise ValueError('Conta fixa inválida (dia entre 1 e 30).')
        day_val = int(day)
        wk_val = None
        if isinstance(last_generated, str):
            last = last_generated
        else:
            # Se o dia deste mês já passou, a 1ª conta vem só no próximo mês (não nasce atrasada).
            last = now().strftime('%Y-%m') if day_val < now().day else None
    with conn() as c:
        return c.execute('INSERT INTO recurring_bills(title,amount,day,last_generated,weekday) VALUES(?,?,?,?,?)',
                         (title.strip()[:180], amount, day_val, last, wk_val)).lastrowid


def generate_recurring(month=None):
    """Cria a conta do mês para cada conta fixa ativa. Idempotente: roda quantas vezes quiser."""
    month = month or now().strftime('%Y-%m')
    year, month_num = (int(x) for x in month.split('-'))
    max_day = calendar.monthrange(year, month_num)[1]
    today = now().date()
    created = []
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('''SELECT * FROM recurring_bills
            WHERE active=1 AND (last_generated IS NULL OR last_generated < ?)''', (month,)).fetchall()
        for r in rows:
            if r['weekday'] is not None:
                for d_num in range(1, max_day + 1):
                    dt = date(year, month_num, d_num)
                    if dt.weekday() == r['weekday']:
                        if r['last_generated'] is None and month == today.strftime('%Y-%m') and dt <= today:
                            continue
                        due = dt.isoformat()
                        exists = c.execute('SELECT 1 FROM debts WHERE title=? AND original_amount=? AND due_date=?',
                                           (r['title'], r['amount'], due)).fetchone()
                        if not exists:
                            cur = c.execute('INSERT INTO debts(title,original_amount,due_date,created_at) VALUES(?,?,?,?)',
                                            (r['title'], r['amount'], due, now().isoformat()))
                            created.append(cur.lastrowid)
            else:
                actual_day = min(int(r['day']), max_day)
                due = f"{month}-{actual_day:02d}"
                exists = c.execute('SELECT 1 FROM debts WHERE title=? AND original_amount=? AND due_date=?',
                                   (r['title'], r['amount'], due)).fetchone()
                if not exists:
                    cur = c.execute('INSERT INTO debts(title,original_amount,due_date,created_at) VALUES(?,?,?,?)',
                                    (r['title'], r['amount'], due, now().isoformat()))
                    created.append(cur.lastrowid)
            c.execute('UPDATE recurring_bills SET last_generated=? WHERE id=?', (month, r['id']))
    return created


def get_recurring(active_only=True):
    sql = 'SELECT * FROM recurring_bills'
    if active_only:
        sql += ' WHERE active=1'
    sql += ' ORDER BY day, id'
    with conn() as c:
        return [dict(r) for r in c.execute(sql)]


def remove_recurring(rec_id):
    with conn() as c:
        if c.execute('DELETE FROM recurring_bills WHERE id=?', (rec_id,)).rowcount == 0:
            raise ValueError(f'Conta fixa #{rec_id} não encontrada.')


def update_recurring(rec_id, new_amount, update_open_debts=True):
    if new_amount <= 0:
        raise ValueError('Valor inválido.')
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT * FROM recurring_bills WHERE id=?', (rec_id,)).fetchone()
        if not r:
            raise ValueError(f'Conta fixa #{rec_id} não encontrada.')
        c.execute('UPDATE recurring_bills SET amount=? WHERE id=?', (new_amount, rec_id))
        if update_open_debts:
            c.execute('UPDATE debts SET original_amount=? WHERE title=? AND paid_amount=0',
                      (new_amount, r['title']))
        return dict(r, amount=new_amount)


def reset_all():
    """Zera todos os registros financeiros, contas, lembretes, perfil e histórico."""
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        for table in ('debt_payments', 'debt_notifications', 'expected_incomes', 'transactions', 'debts',
                      'recurring_bills', 'fixed_incomes', 'reminders', 'messages', 'preferences'):
            c.execute(f'DELETE FROM {table}')
        c.execute('DELETE FROM sqlite_sequence')
