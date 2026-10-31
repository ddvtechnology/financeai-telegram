"""Persistência SQLite em centavos, transações ACID e consultas verificáveis."""
import calendar
import contextlib
import re
import sqlite3
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
        ''')
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


def add_debt(amount, due_date, title):
    if amount <= 0 or not title.strip():
        raise ValueError('Conta inválida')
    _check_date_window(date.fromisoformat(due_date))
    with conn() as c:
        cur = c.execute('''INSERT INTO debts(title,original_amount,due_date,created_at)
            VALUES(?,?,?,?)''',(title.strip()[:180], amount, due_date, now().isoformat()))
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
                        'remaining': d['original_amount'] - d['paid_amount']})
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
    debts = [dict(d, remaining=d['original_amount'] - d['paid_amount'],
                  days_left=(date.fromisoformat(d['due_date']) - today).days)
             for d in get_debts() if d['due_date'] <= limit]
    reminders = [r for r in get_reminders()
                 if datetime.fromisoformat(r['due_at']).astimezone(TZ).date() == today]
    return {'summary': summary(), 'debts': debts, 'reminders': reminders}


def ai_context():
    s = summary()
    debts = get_debts(open_only=True)
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

    recurring = get_recurring()
    if recurring:
        w_names = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo')
        lines.append('')
        lines.append('Contas fixas cadastradas:')
        for r in recurring:
            when_str = f"toda {w_names[r['weekday']]}" if r.get('weekday') is not None else f"todo dia {r['day']:02d}"
            lines.append(f"- #{r['id']} {r['title']}: {brl(r['amount'])} ({when_str})")

    lines.append('')
    lines.append('Dívidas/contas abertas (organizadas por mês de vencimento):')
    if debts:
        cur_m = None
        for d in debts:
            m_due = d['due_date'][:7]
            if m_due != cur_m:
                cur_m = m_due
                lines.append(f"  [Vencimento em {m_due[5:]}/{m_due[:4]}]:")
            rem = d['original_amount'] - d['paid_amount']
            norm = unicodedata.normalize('NFKD', d['title'].lower()).encode('ascii', 'ignore').decode('ascii').strip()
            note = " (regra: abatimento mensal de R$ 100 todo dia 30, não é o saldo total de uma vez)" if norm in ('perfumes', 'roupas ads multimarcas') else ""
            lines.append(f"  - #{d['id']} {d['title']}: falta {brl(rem)}; vence {short_date(d['due_date'])}{note}")
    else:
        lines.append('- Nenhuma cadastrada')
    lines.append('')
    lines.append('Últimas movimentações:')
    lines += [f"- {short_date(t['occurred_at'])} | {t['kind']}: {brl(t['amount'])}, {t['category']}, {t['description'][:60]}" for t in tx] or ['- Nenhuma cadastrada']
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
        for table in ('debt_payments', 'debt_notifications', 'transactions', 'debts',
                      'recurring_bills', 'reminders', 'messages', 'preferences'):
            c.execute(f'DELETE FROM {table}')
        c.execute('DELETE FROM sqlite_sequence')
