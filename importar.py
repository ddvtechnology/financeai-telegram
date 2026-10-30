"""Script para importar dados financeiros estruturados em JSON (gerados pelo ChatGPT ou outros)
diretamente para a base SQLite do FinanceAI.

Uso:
    python importar.py dados.json
    python importar.py  (lê o arquivo importar.json por padrão se existir)
"""
import json
import sys
from pathlib import Path
from datetime import date
import db

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def import_financial_data(filepath):
    path = Path(filepath)
    if not path.is_file():
        print(f"Erro: Arquivo '{filepath}' não encontrado.")
        sys.exit(1)

    try:
        content = path.read_text(encoding='utf-8')
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"Erro ao ler JSON: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"Erro ao abrir arquivo: {exc}")
        sys.exit(1)

    db.init()
    if data.get('limpar_antes'):
        db.reset_all()

    transactions = data.get('transacoes') or (data.get('perfil', {}).get('transacoes') if isinstance(data.get('perfil'), dict) else []) or []
    debts = data.get('contas_a_pagar') or (data.get('perfil', {}).get('contas_a_pagar') if isinstance(data.get('perfil'), dict) else []) or []
    recurring = data.get('contas_fixas') or (data.get('perfil', {}).get('contas_fixas') if isinstance(data.get('perfil'), dict) else []) or []

    if not transactions and not debts and not recurring:
        print("Aviso: Nenhuma transação ('transacoes'), conta ('contas_a_pagar') ou fixa ('contas_fixas') encontrada no JSON.")
        return {'tx': 0, 'debts': 0, 'recurring': 0, 'profile': 0, 'errors': []}

    print("=" * 60)
    print(f"Iniciando importação de: {path.name}")
    print(f"Transações encontradas: {len(transactions)}")
    print(f"Contas/Dívidas encontradas: {len(debts)}")
    if recurring:
        print(f"Contas fixas encontradas: {len(recurring)}")
    print("=" * 60)

    count_tx = 0
    count_debts = 0
    count_rec = 0
    errors = []
    skipped = []

    # Importar transações (receitas e despesas)
    for idx, tx in enumerate(transactions, 1):
        try:
            kind = str(tx.get('tipo', '')).strip().lower()
            if kind not in ('receita', 'despesa'):
                raise ValueError(f"Tipo inválido: '{kind}' (deve ser 'receita' ou 'despesa')")

            amount_raw = tx.get('valor')
            amount = db.money_to_cents(amount_raw)

            category = str(tx.get('categoria', 'Outros')).strip() or 'Outros'
            description = str(tx.get('descricao', category)).strip()
            occurred_date = tx.get('data')

            if occurred_date:
                # Validar formato de data
                date.fromisoformat(str(occurred_date))

            if occurred_date and db.transaction_exists(kind, amount, category, description, str(occurred_date)):
                skipped.append(f"Transação #{idx} já existe, ignorada: {category} {db.brl(amount)} em {occurred_date}")
                continue
            txid = db.insert_transaction(
                kind=kind,
                amount=amount,
                category=category,
                description=description,
                occurred_date=occurred_date
            )
            count_tx += 1
            print(f"  [Tx #{txid}] {kind.capitalize():7} {db.brl(amount):>12} | {category} - {description}")
        except Exception as exc:
            errors.append(f"Transação #{idx} ({tx}): {exc}")

    # Importar contas a pagar
    for idx, d in enumerate(debts, 1):
        try:
            title = str(d.get('titulo', '')).strip()
            if not title:
                raise ValueError("Título da conta é obrigatório")

            total_raw = d.get('valor_total') or d.get('valor')
            amount = db.money_to_cents(total_raw)

            due_date = str(d.get('vencimento', '')).strip()
            if not due_date:
                raise ValueError("Data de vencimento é obrigatória (AAAA-MM-DD)")
            date.fromisoformat(due_date)

            with db.conn() as c:
                exists = c.execute('SELECT 1 FROM debts WHERE title=? AND original_amount=? AND due_date=?',
                                   (title[:180], amount, due_date)).fetchone()
            if exists:
                print(f"  [Conta já existe] {title} | {db.brl(amount)} | {db.short_date(due_date)} (ignorada)")
                continue

            paid_raw = d.get('valor_pago')
            paid_amount = 0
            if paid_raw is not None and str(paid_raw).strip() not in ('', '0', '0.0', '0.00', '0,00'):
                paid_amount = db.money_to_cents(paid_raw)
                if paid_amount > amount:
                    raise ValueError(f"Valor pago ({db.brl(paid_amount)}) não pode ser maior que o total ({db.brl(amount)})")

            debt_id = db.add_debt(amount=amount, due_date=due_date, title=title)
            if paid_amount > 0:
                pay_date = d.get('registrar_pagamento_em')
                if pay_date:
                    db.pay_debt(debt_id, paid_amount, occurred_date=str(pay_date))
                    count_tx += 1
                else:
                    with db.conn() as c:
                        c.execute("UPDATE debts SET paid_amount = ? WHERE id = ?", (paid_amount, debt_id))

            count_debts += 1
            print(f"  [Conta #{debt_id}] {title} | Total: {db.brl(amount)} | Vence: {db.short_date(due_date)}")
        except Exception as exc:
            errors.append(f"Conta #{idx} ({d}): {exc}")

    # Importar contas fixas mensais / semanais
    for idx, rec in enumerate(recurring, 1):
        try:
            title = str(rec.get('titulo', '')).strip()
            if not title:
                raise ValueError("Título da conta fixa é obrigatório")
            amount = db.money_to_cents(rec.get('valor'))
            day = rec.get('dia', 0)
            weekday = rec.get('dia_semana')
            last_gen = rec.get('ultimo_mes_gerado', False)
            with db.conn() as c:
                exists = c.execute(
                    'SELECT 1 FROM recurring_bills WHERE title=? AND amount=? AND day=? AND COALESCE(weekday,-1)=COALESCE(?,-1)',
                    (title[:180], amount, int(day or 0), int(weekday) if weekday is not None else None)
                ).fetchone()
            if exists:
                continue
            rec_id = db.add_recurring(title, amount, day=int(day or 0), weekday=weekday, last_generated=last_gen)
            count_rec += 1
            print(f"  [Fixa #{rec_id}] {title} | {db.brl(amount)}")
        except Exception as exc:
            errors.append(f"Conta fixa #{idx} ({rec}): {exc}")

    for m_str in (data.get('gerar_meses') or []):
        try:
            created = db.generate_recurring(str(m_str))
            count_debts += len(created)
        except Exception as exc:
            errors.append(f"Gerar mês {m_str}: {exc}")

    # Importar perfil e diretrizes de planejamento (se houver)
    raw_profile = data.get('perfil') or data.get('metas')
    count_profile = 0
    if isinstance(raw_profile, dict):
        profile = {k: v for k, v in raw_profile.items() if not isinstance(v, (list, dict))}
        db.set_profile(profile)
        count_profile = len(profile)
        print("=" * 60)
        print("PERFIL E METAS IMPORTADOS:")
        for k, v in profile.items():
            print(f"  - {k}: {v}")

    print("=" * 60)
    print("RESUMO DA IMPORTAÇÃO:")
    print(f"✅ Transações importadas com sucesso: {count_tx}")
    print(f"✅ Contas a pagar cadastradas: {count_debts}")
    if count_rec:
        print(f"✅ Contas fixas cadastradas: {count_rec}")
    if count_profile:
        print(f"✅ Metas e diretrizes de perfil salvas: {count_profile} campos")
    if skipped:
        print(f"↩️ Já existentes (ignoradas): {len(skipped)}")
    if errors:
        print(f"⚠️ Erros/avisos ({len(errors)}):")
        for err in errors:
            print(f"   - {err}")

    # Resumo atual do banco
    s = db.summary()
    print("-" * 60)
    print(f"Mês atual ({s['month']}): Receitas: {db.brl(s['receita'])} | Despesas: {db.brl(s['despesa'])}")
    print(f"Saldo histórico registrado: {db.brl(s['total'])}")
    print("=" * 60)
    return {'tx': count_tx, 'debts': count_debts, 'recurring': count_rec, 'profile': count_profile, 'errors': errors}


if __name__ == '__main__':
    default_file = 'importar.json' if Path('importar.json').is_file() else 'dados_iniciais.json'
    target = sys.argv[1] if len(sys.argv) > 1 else default_file
    import_financial_data(target)
