"""Persistência SQLite em centavos, transações ACID e consultas verificáveis."""
import contextlib
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
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
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=15000')
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
                day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 28),
                active INTEGER NOT NULL DEFAULT 1,
                last_generated TEXT
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
        ''')


def insert_transaction(kind, amount, category, description='', occurred_date=None):
    if kind not in ('receita', 'despesa') or amount <= 0 or not category.strip():
        raise ValueError('Lançamento inválido')
    stamp = now().isoformat()
    if occurred_date:
        try:
            day = date.fromisoformat(str(occurred_date))
        except ValueError:
            raise ValueError('Data do lançamento inválida.') from None
        current = now()
        stamp = datetime.combine(day, current.timetz().replace(tzinfo=None), tzinfo=TZ).isoformat()
    with conn() as c:
        result = c.execute('''INSERT INTO transactions(kind,amount,category,description,occurred_at)
            VALUES(?,?,?,?,?)''', (kind, amount, category.strip()[:70], description.strip()[:250], stamp))
        return result.lastrowid


def add_debt(amount, due_date, title):
    if amount <= 0 or not title.strip() or date.fromisoformat(due_date) is None:
        raise ValueError('Conta inválida')
    with conn() as c:
        cur = c.execute('''INSERT INTO debts(title,original_amount,due_date,created_at)
            VALUES(?,?,?,?)''',(title.strip()[:180], amount, due_date, now().isoformat()))
        return cur.lastrowid


def pay_debt(debt_id, amount):
    """Pagamento e despesa ocorrem na MESMA transação, sem dupla contagem."""
    if amount <= 0: raise ValueError('O valor precisa ser positivo.')
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d: raise ValueError('Dívida não encontrada.')
        pending = d['original_amount'] - d['paid_amount']
        if amount > pending: raise ValueError(f'Valor acima do restante: {brl(pending)}.')
        c.execute('UPDATE debts SET paid_amount=paid_amount+? WHERE id=?', (amount, debt_id))
        stamp = now().isoformat()
        tx = c.execute('''INSERT INTO transactions(kind,amount,category,description,occurred_at,debt_id)
            VALUES ('despesa',?,?,?,?,?)''', (amount, 'Pagamento de dívida', d['title'], stamp, debt_id)).lastrowid
        c.execute('''INSERT INTO debt_payments(debt_id,transaction_id,amount,paid_at)
            VALUES (?,?,?,?)''', (debt_id,tx,amount,stamp))
        return pending - amount


def get_debts(open_only=True):
    sql = 'SELECT * FROM debts'
    if open_only: sql += ' WHERE original_amount > paid_amount'
    sql += ' ORDER BY due_date, id'
    with conn() as c:
        return [dict(r) for r in c.execute(sql)]


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


def transaction_exists(kind, amount, category, description, day):
    """Usado pela importação para não duplicar lançamentos idênticos no mesmo dia."""
    with conn() as c:
        return c.execute('''SELECT 1 FROM transactions WHERE kind=? AND amount=? AND category=?
            AND description=? AND substr(occurred_at,1,10)=? LIMIT 1''',
            (kind, amount, category.strip()[:70], description.strip()[:250], day)).fetchone() is not None


def get_transactions(limit=15):
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT ?', (limit,))]


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


def planning():
    current = now().strftime('%Y-%m')
    s = summary(current)
    # Projeção simples: vencimentos abertos futuros, não inventa receita futura.
    today = now().date().isoformat()
    next_m = next_month(current)
    with conn() as c:
        due = c.execute('''SELECT COALESCE(SUM(original_amount-paid_amount),0) FROM debts
              WHERE due_date>=? AND due_date<? AND original_amount>paid_amount''',(today, next_m+'-01')).fetchone()[0]
        following = c.execute('''SELECT COALESCE(SUM(original_amount-paid_amount),0) FROM debts
              WHERE due_date>=? AND due_date<? AND original_amount>paid_amount''',(next_m+'-01',next_month(next_m)+'-01')).fetchone()[0]
    return {'month':current,'balance':s['total'], 'income_month':s['receita'], 'spent_month':s['despesa'],
            'due_remaining_month':due,'due_next_month':following}


def insert_reminder(due_iso, content):
    if not content.strip(): raise ValueError('Descrição obrigatória.')
    with conn() as c:
        return c.execute('INSERT INTO reminders(due_at,content) VALUES(?,?)',
                         (due_iso,content.strip()[:300])).lastrowid


def due_reminders():
    with conn() as c:
        return [dict(r) for r in c.execute('''SELECT * FROM reminders
          WHERE sent_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT 20''',(now().isoformat(),))]


def mark_reminder(rid):
    with conn() as c:
        c.execute('UPDATE reminders SET sent_at=? WHERE id=? AND sent_at IS NULL',(now().isoformat(),rid))


def get_reminders():
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM reminders WHERE sent_at IS NULL ORDER BY due_at LIMIT 15')]


def set_mode(mode):
    if mode not in ('auto','rapido','avancado'): raise ValueError('Modo inválido')
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


def last_messages(n=8):
    with conn() as c:
        rows = list(c.execute('SELECT role,content FROM messages ORDER BY id DESC LIMIT ?',(n,)))
    return [dict(row) for row in reversed(rows)]


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


def ai_context():
    s = summary()
    debts = get_debts()[:20]
    tx = get_transactions(15)
    profile = get_profile()
    lines = [
        f"Mês atual: {s['month']}",
        f"Receitas registradas no mês: {brl(s['receita'])}",
        f"Despesas registradas no mês: {brl(s['despesa'])}",
        f"Saldo histórico registrado (todas as movimentações): {brl(s['total'])}",
    ]

    # Perfil estruturado — dados de renda e planejamento
    if profile:
        lines.append('')
        lines.append('=== PERFIL FINANCEIRO DO USUÁRIO (DADOS CONFIRMADOS) ===')
        if 'renda_fixa' in profile:
            lines.append('RENDA FIXA MENSAL (TODOS OS MESES):')
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

    lines.append('')
    lines.append('Dívidas/contas abertas:')
    lines += [f"- #{d['id']} {d['title']}: falta {brl(d['original_amount']-d['paid_amount'])}; vence {short_date(d['due_date'])}" for d in debts] or ['- Nenhuma cadastrada']
    lines.append('')
    lines.append('Últimas movimentações:')
    lines += [f"- {t['kind']}: {brl(t['amount'])}, {t['category']}, {t['description'][:60]}" for t in tx] or ['- Nenhuma cadastrada']
    return '\n'.join(lines)



def add_recurring(title, amount, day):
    if amount <= 0 or not title.strip() or not 1 <= int(day) <= 28:
        raise ValueError('Conta fixa inválida (dia entre 1 e 28).')
    with conn() as c:
        return c.execute('INSERT INTO recurring_bills(title,amount,day) VALUES(?,?,?)',
                         (title.strip()[:180], amount, int(day))).lastrowid


def generate_recurring(month=None):
    """Cria a conta do mês para cada conta fixa ativa. Idempotente: roda quantas vezes quiser."""
    month = month or now().strftime('%Y-%m')
    created = []
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('''SELECT * FROM recurring_bills
            WHERE active=1 AND (last_generated IS NULL OR last_generated < ?)''', (month,)).fetchall()
        for r in rows:
            due = f"{month}-{r['day']:02d}"
            cur = c.execute('INSERT INTO debts(title,original_amount,due_date,created_at) VALUES(?,?,?,?)',
                            (r['title'], r['amount'], due, now().isoformat()))
            c.execute('UPDATE recurring_bills SET last_generated=? WHERE id=?', (month, r['id']))
            created.append(cur.lastrowid)
    return created
