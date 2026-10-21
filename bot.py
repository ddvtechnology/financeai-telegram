"""FinanceAI Telegram. Não realiza transferências; registros somente locais no SQLite."""
import asyncio
import csv
import html
try:
    import fcntl
except ImportError:
    fcntl = None
import io
import logging
import re
import secrets
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, date
from pathlib import Path
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup)
from telegram.constants import ChatType
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters)
import ai
import db
from config import BASE, DB_PATH, OWNER_ID, TOKEN, TZ

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('financeai')
LOCK = None

HELP = '''FinanceAI — seu assistente financeiro pessoal

Converse normalmente, como falaria com uma pessoa. Exemplos:
• Gastei R$ 45,90 no almoço.
• Recebi R$ 1.870 de salário.
• Tenho uma conta de luz de R$ 120 para pagar dia 28.
• Paguei R$ 100 do aluguel.
• Me lembra de pagar o aluguel dia 28 às 9h.
• Quanto gastei este mês?
• Quais contas ainda faltam pagar?
• Me ajude a organizar meu orçamento.

Antes de salvar receita, despesa, conta, pagamento ou lembrete, vou mostrar os dados e pedir sua confirmação. Nada é registrado sem você tocar em Confirmar.

Use os poucos botões abaixo como atalhos; o restante pode ser escrito em linguagem natural.

Importante: o saldo é calculado com os registros deste bot, não é o saldo real de uma conta bancária. O bot não faz PIX nem pagamentos reais.'''


def allowed(update):
    return (update.effective_chat is not None and update.effective_chat.type == ChatType.PRIVATE
            and update.effective_user is not None and OWNER_ID > 0
            and update.effective_user.id == OWNER_ID)


async def guard(update):
    if allowed(update): return True
    if update.callback_query:
        await update.callback_query.answer('Não autorizado.', show_alert=True)
    elif update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return False
    elif update.message:
        await update.message.reply_text('Acesso restrito. Envie /id para descobrir seu ID e configure TELEGRAM_OWNER_ID no arquivo .env.')
    return False


def clean_telegram_markdown(text):
    if not text: return text
    # Converte marcadores de lista iniciados por asterisco ou traço (* item ou - item) em bullets (• item)
    text = re.sub(r'(?m)^\s*[\*\-]\s+', '• ', text)
    # Converte negrito padrão markdown (**texto**) em negrito do Telegram (*texto*)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Remove asteriscos órfãos com espaços ao redor (ex: " * " ou " *")
    text = re.sub(r'(?<=\s)\*(?=\s)', '•', text)
    # Se houver número ímpar de asteriscos *, remove o último asterisco solto para não quebrar o parser
    if text.count('*') % 2 != 0:
        idx = text.rfind('*')
        text = text[:idx] + text[idx+1:]
    # Escapa underscores isolados dentro de palavras (ex: variavel_nome -> variavel\_nome)
    text = re.sub(r'(?<=\w)_(?=\w)', r'\_', text)
    if text.count('_') % 2 != 0:
        idx = text.rfind('_')
        text = text[:idx] + text[idx+1:]
    return text


async def say(update, text, keyboard=None, parse_mode='Markdown'):
    """Divide respostas mantendo o teclado persistente e suportando formatação Markdown/HTML."""
    markup = keyboard if keyboard is not None else main_keyboard()
    if parse_mode == 'Markdown':
        text = clean_telegram_markdown(text)
    chunks = [text[start:start+3900] for start in range(0, len(text), 3900)] or [text]
    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        k = markup if is_last else None
        try:
            await update.message.reply_text(chunk, reply_markup=k, parse_mode=parse_mode)
        except Exception:
            # Fallback se a mensagem tiver formatação inválida
            await update.message.reply_text(chunk, reply_markup=k)


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton('💰 Saldo'), KeyboardButton('➕ Registrar receita')],
            [KeyboardButton('➖ Registrar despesa'), KeyboardButton('📅 Contas a pagar')],
            [KeyboardButton('📊 Relatório'), KeyboardButton('⚙️ Modelo de IA')],
        ], resize_keyboard=True, is_persistent=True,
        input_field_placeholder='Conte o que aconteceu com seu dinheiro...'
    )


def normalized(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def looks_financial(text):
    plain = normalized(text)
    return bool(
        re.search(r'\b(gastei|gasto|despesa|comprei|paguei|pagamento|quitei|recebi|ganhei|salario|renda|receita|conta a pagar|contas a pagar|vencimento|vence|divida|dividas|saldo|relatorio|historico|planejamento|orcamento|lembrete|lembra|boleto|fatura|cartao|aluguel|supermercado|mercado|alimentacao|transporte|juros|financiamento)\b', plain)
        or re.search(r'R\$', text, re.I)
        or re.search(r'\b\d+[,.]\d{2}\b', text)
    )


async def store_clarification(update, context, prompt, result, question, previous=None):
    user_id = update.effective_user.id
    pending = context.application.bot_data.setdefault('natural_drafts', {})
    original = (previous or {}).get('original_message', prompt)
    partial = {k: v for k, v in result.items() if not str(k).startswith('_')}
    pending[user_id] = {
        'original_message': original,
        'partial': partial,
        'updated_at': db.now().timestamp(),
    }
    await say(update, question)


def purge_expired(store, ttl=600):
    limit = db.now().timestamp() - ttl
    for key in [k for k, v in store.items() if (v.get('created_at') if isinstance(v, dict) else v[2]) < limit]:
        store.pop(key, None)


async def ask_action_confirmation(update, context, action, message):
    nonce = secrets.token_hex(8)
    pending = context.application.bot_data.setdefault('natural_actions', {})
    purge_expired(pending)
    pending[nonce] = {'action': action, 'created_at': db.now().timestamp()}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Confirmar registro', callback_data='nl:confirm:' + nonce),
        InlineKeyboardButton('Cancelar', callback_data='nl:cancel:' + nonce),
    ]])
    await update.message.reply_text(message, reply_markup=keyboard)


async def show_id(update, context):
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(f'Seu ID numérico Telegram é: {update.effective_user.id}\nGuarde-o para configurar TELEGRAM_OWNER_ID no .env.')


async def start(update, context):
    if not await guard(update): return
    await update.message.reply_text('FinanceAI está pronto. Vamos cuidar das suas finanças conversando normalmente.\n\n' + HELP, reply_markup=main_keyboard())


async def help_cmd(update, context):
    if not await guard(update): return
    await update.message.reply_text(HELP, reply_markup=main_keyboard())


def parse_movement(text):
    # /despesa 20,00 Transporte | Táxi
    match = re.match(r'^/\w+(?:@\w+)?\s+([^\s]+)\s+([^|\n]+?)(?:\s*\|\s*(.*))?\s*$', text, re.S)
    if not match: raise ValueError('Formato: /despesa 45,90 Alimentação | Almoço')
    return db.money_to_cents(match.group(1)),match.group(2).strip(),(match.group(3) or '').strip()


async def movement(update, context):
    if not await guard(update): return
    try:
        amount, category, description = parse_movement(update.message.text)
        kind = 'receita' if update.message.text.split()[0].split('@')[0] == '/receita' else 'despesa'
        action = {'type':'transaction','kind':kind,'amount':amount,'category':category,
                  'description':description,'occurred_date':None}
        await ask_action_confirmation(update, context, action,
            f'Confira antes de registrar:\n\nTipo: {kind.capitalize()}\nValor: {db.brl(amount)}\nCategoria: {category}' + (f'\nDescrição: {description}' if description else '') + '\n\nPosso salvar no seu histórico financeiro?')
    except ValueError as exc: await say(update,str(exc))


async def tx_delete(update, context):
    if not await guard(update): return
    m = re.match(r'^/apagar(?:@\w+)?\s+(\d+)\s*$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /apagar <id do lançamento>\nVeja os ids em /historico.'); return
    tx = db.get_transaction(int(m.group(1)))
    if not tx:
        await say(update, 'Lançamento não encontrado.'); return
    if tx['debt_id']:
        await say(update, 'Este lançamento é pagamento de dívida e não pode ser apagado por aqui.'); return
    action = {'type': 'tx_delete', 'tx_id': tx['id']}
    await ask_action_confirmation(update, context, action,
        f'Apagar este lançamento?\n\n#{tx["id"]} {tx["kind"].capitalize()} {db.brl(tx["amount"])}\n'
        f'{tx["category"]} — {tx["description"]}\nData: {db.short_date(tx["occurred_at"])}')


async def recurring_add(update, context):
    if not await guard(update): return
    m = re.match(r'^/fixa(?:@\w+)?\s+(\S+)\s+(\d{1,2})\s+(.+)$', update.message.text.strip(), re.S)
    if not m:
        await say(update, 'Formato: /fixa <valor> <dia do mês 1-28> <nome>\nExemplo: /fixa 570 10 Faculdade'); return
    try:
        rid = db.add_recurring(m.group(3), db.money_to_cents(m.group(1)), int(m.group(2)))
        created = db.generate_recurring()
        extra = ' Já gerei a conta deste mês.' if created else ''
        await say(update, f'✅ Conta fixa #{rid} cadastrada: {m.group(3).strip()} — todo dia {int(m.group(2))}.{extra}')
    except ValueError as exc:
        await say(update, str(exc))


async def balance(update, context):
    if not await guard(update): return
    s = db.summary()
    year, month_num = s["month"].split('-')
    month_br = f'{month_num}/{year}'
    await say(update,
        f'💰 *Saldo Atual Registrado:* {db.brl(s["total"])}\n\n'
        f'📅 *Mês de Referência:* {month_br}\n'
        f'🟢 *Receitas:* {db.brl(s["receita"])}\n'
        f'🔴 *Despesas:* {db.brl(s["despesa"])}'
    )


async def debt_add(update, context):
    if not await guard(update): return
    m = re.match(r'^/conta(?:@\w+)?\s+(\S+)\s+(\d{2}/\d{2}/\d{4})\s+(.+)$', update.message.text, re.S)
    if not m:
        await say(update,'Formato: /conta 200 28/10/2026 Aluguel');return
    try:
        amount=db.money_to_cents(m.group(1))
        due=db.parse_date(m.group(2))
        title=m.group(3).strip()
        action={'type':'debt','amount':amount,'due_date':due,'title':title}
        await ask_action_confirmation(update, context, action,
            f'Confira a conta antes de salvar:\n\nConta: {title}\nValor: {db.brl(amount)}\nVencimento: {db.short_date(due)}\n\nPosso adicionar à sua lista de contas a pagar?')
    except ValueError as exc:await say(update,f'Dados inválidos: {exc}')


async def debt_list(update, context):
    if not await guard(update): return
    debts = db.get_debts()
    if not debts:
        await say(update, '🎉 Parabéns! Nenhuma conta ou dívida em aberto no momento.')
        return
    total = sum(d['original_amount'] - d['paid_amount'] for d in debts)
    lines = ['📅 <b>CONTAS E DÍVIDAS EM ABERTO:</b>\n']
    for d in debts[:40]:
        rem = d["original_amount"] - d["paid_amount"]
        due_br = db.short_date(d["due_date"])
        title = html.escape(str(d["title"]))
        lines.append(f'📌 <b>#{d["id"]} {title}</b>\n   Restante: <code>{html.escape(db.brl(rem))}</code> | 🗓 Vencimento: <code>{due_br}</code>\n')
    if len(debts) > 40:
        lines.append(f'... e mais {len(debts) - 40} contas.\n')
    lines.append(f'💳 <b>Total geral em aberto:</b> <code>{html.escape(db.brl(total))}</code>')
    await say(update, '\n'.join(lines), parse_mode='HTML')


async def debt_edit(update, context):
    if not await guard(update): return
    # /corrigir_conta 9 164.00
    m = re.match(r'^/corrigir_conta(?:@\w+)?\s+(\d+)\s+([^\s]+)(?:\s+(\d{2}/\d{2}/\d{4}))?$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /corrigir_conta <id> <novo_valor_restante>\nExemplo: /corrigir_conta 9 164,00\nOu com data: /corrigir_conta 9 164,00 05/11/2026')
        return
    try:
        debt_id = int(m.group(1))
        amount = db.money_to_cents(m.group(2))
        due_iso = db.parse_date(m.group(3)) if m.group(3) else None
        
        # Buscar dívida para manter histórico de pagamento se houver
        with db.conn() as c:
            d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d:
            raise ValueError(f'Dívida #{debt_id} não encontrada.')
        
        # Se ela já tem valor pago, o novo total = valor_pago + novo_restante
        new_total = d['paid_amount'] + amount
        remaining = db.update_debt(debt_id, new_total, new_due_date=due_iso)
        await say(update, f'✅ Conta #{debt_id} ({d["title"]}) atualizada!\nValor restante corrigido para: {db.brl(remaining)}')
    except ValueError as exc:
        await say(update, f'Erro ao corrigir conta: {exc}')


async def debt_delete(update, context):
    if not await guard(update): return
    # /excluir_conta 9
    m = re.match(r'^/excluir_conta(?:@\w+)?\s+(\d+)$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /excluir_conta <id>\nExemplo: /excluir_conta 9')
        return
    try:
        debt_id = int(m.group(1))
        db.remove_debt(debt_id)
        await say(update, f'✅ Conta #{debt_id} excluída com sucesso.')
    except ValueError as exc:
        await say(update, f'Não foi possível excluir: {exc}')


async def pay(update, context):
    if not await guard(update):return
    m = re.match(r'^/pagar(?:@\w+)?\s+(\d+)\s+([^\s]+)\s*$',update.message.text)
    if not m:await say(update,'Formato: /pagar 1 100');return
    try:
        debt_id=int(m.group(1));amount=db.money_to_cents(m.group(2))
        d=next((row for row in db.get_debts() if row['id']==debt_id),None)
        if not d: raise ValueError('Dívida em aberto não encontrada.')
        if amount>d['original_amount']-d['paid_amount']:raise ValueError('Valor maior que a dívida restante.')
        nonce=secrets.token_hex(8)
        pending=context.application.bot_data.setdefault('pending_payments',{})
        pending[nonce]=(debt_id,amount,db.now().timestamp())
        keyboard=InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Confirmar registro', callback_data='pay:'+nonce),
            InlineKeyboardButton('Cancelar', callback_data='cancel:'+nonce)]])
        await update.message.reply_text(f'Confirmar pagamento REGISTRADO de {db.brl(amount)} da dívida #{debt_id} ({d["title"]})?\nNão transfere dinheiro de verdade.',reply_markup=keyboard)
    except ValueError as exc:await say(update,str(exc))


async def payment_callback(update, context):
    q=update.callback_query
    if not await guard(update):return
    await q.answer()
    action,nonce=q.data.split(':',1)
    pending=context.application.bot_data.setdefault('pending_payments',{})
    entry=pending.pop(nonce,None)
    if not entry or db.now().timestamp()-entry[2]>600:
        await q.edit_message_text('Confirmação vencida ou já utilizada. Envie /pagar novamente.');return
    if action=='cancel':await q.edit_message_text('Pagamento cancelado.');return
    try:
        debt_id,amount,_=entry
        remaining=db.pay_debt(debt_id,amount)
        await q.edit_message_text(f'✅ Pagamento registrado: {db.brl(amount)}\nRestante: {db.brl(remaining)}\nSaldo registrado: {db.brl(db.summary()["total"])}')
    except ValueError as exc:await q.edit_message_text(f'Não registrado: {exc}')


async def history(update, context):
    if not await guard(update):return
    transactions=db.get_transactions(20)
    if not transactions:await say(update,'Não há movimentações registradas.');return
    lines=['ÚLTIMAS MOVIMENTAÇÕES:']
    for t in transactions:
        emoji='➕' if t['kind']=='receita' else '➖'
        lines.append(f'{emoji} #{t["id"]} {db.short_date(t["occurred_at"])} {db.brl(t["amount"])} | {t["category"]} | {t["description"]}\n')
    await say(update, '\n'.join(lines))


async def report(update, context):
    if not await guard(update): return
    month = context.args[0] if context.args else db.now().strftime('%Y-%m')
    try:
        s = db.summary(month)
    except ValueError as exc:
        await say(update, str(exc))
        return

    year, month_num = month.split('-')
    month_br = f'{month_num}/{year}'

    lines = [
        f'📊 *RELATÓRIO FINANCEIRO — {month_br}*\n',
        f'🟢 *Total de Receitas:* `{db.brl(s["receita"])}`',
        f'🔴 *Total de Despesas:* `{db.brl(s["despesa"])}`',
        f'💰 *Saldo em Caixa:* `{db.brl(s["total"])}`\n',
        '📂 *Maiores Gastos por Categoria:*'
    ]
    if s['categories']:
        for c in s['categories']:
            lines.append(f'  • {c["category"]}: `{db.brl(c["total"])}`')
    else:
        lines.append('  • Nenhum gasto registrado neste mês.')

    await say(update, '\n'.join(lines))

    with db.conn() as c:
        rows = c.execute('''SELECT id, kind, amount, category, description, occurred_at FROM transactions
                            WHERE substr(occurred_at,1,7)=? ORDER BY id''', (month,)).fetchall()
    if rows:
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['ID', 'Tipo', 'Valor (R$)', 'Categoria', 'Descrição', 'Data'])
        for r in rows:
            dt_iso = r['occurred_at'][:10]
            dt_br = db.short_date(dt_iso)
            val_fmt = f'{r["amount"] / 100:.2f}'.replace('.', ',')
            writer.writerow([r['id'], r['kind'].capitalize(), val_fmt, r['category'], r['description'], dt_br])
        doc = io.BytesIO(output.getvalue().encode('utf-8-sig'))
        doc.name = f'relatorio-{month_br.replace("/", "-")}.csv'
        await update.message.reply_document(doc, filename=doc.name, caption=f'📄 Extrato detalhado em planilha ({month_br})')


async def plan(update, context):
    if not await guard(update):return
    p=db.planning()
    await say(update,f'PLANEJAMENTO SIMPLIFICADO ({p["month"]})\nSaldo histórico registrado: {db.brl(p["balance"])}\nRecebido neste mês: {db.brl(p["income_month"])}\nGasto neste mês: {db.brl(p["spent_month"])}\nDívidas ainda a vencer neste mês: {db.brl(p["due_remaining_month"])}\nDívidas a vencer no próximo mês: {db.brl(p["due_next_month"])}\n\n⚠️ Não prevê receitas futuras nem é saldo bancário. Para aconselhamento detalhado, escreva: Analise meu planejamento e minhas dívidas.')


async def reminder_add(update, context):
    if not await guard(update):return
    m=re.match(r'^/lembrete(?:@\w+)?\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s+(.+)$',update.message.text,re.S)
    if not m:await say(update,'Formato: /lembrete 28/10/2026 09:00 Pagar aluguel');return
    try:
        dt=datetime.strptime(m.group(1)+' '+m.group(2),'%d/%m/%Y %H:%M').replace(tzinfo=TZ)
        if dt<=db.now():raise ValueError('Informe data e hora futuras.')
        action={'type':'reminder','due_at':dt.isoformat(),'content':m.group(3)}
        await ask_action_confirmation(update, context, action,
            f'Confira o lembrete:\n\nLembrar: {m.group(3)}\nQuando: {m.group(1)} às {m.group(2)}\n\nPosso salvar esse lembrete?')
    except ValueError as exc:await say(update,'Data inválida: '+str(exc))


async def reminder_list(update, context):
    if not await guard(update):return
    rows=db.get_reminders()
    if not rows:await say(update,'Não há lembretes pendentes.');return
    await say(update,'LEMBRETES:\n'+'\n'.join(f'#{r["id"]} {datetime.fromisoformat(r["due_at"]).strftime("%d/%m/%Y %H:%M")} — {r["content"]}' for r in rows))


def mode_keyboard():
    current = db.get_mode()
    auto_icon = '🔘 ' if current == 'auto' else ''
    rapido_icon = '🔘 ' if current == 'rapido' else ''
    avancado_icon = '🔘 ' if current == 'avancado' else ''
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f'{auto_icon}⚡ Automático (Recomendado)', callback_data='mode:auto')],
        [InlineKeyboardButton(f'{rapido_icon}🚀 Rápido (Lightning - 30B)', callback_data='mode:rapido')],
        [InlineKeyboardButton(f'{avancado_icon}🧠 Avançado (Ultra - 550B)', callback_data='mode:avancado')],
    ])


async def mode_set(update, context):
    if not await guard(update): return
    if not context.args:
        current = db.get_mode()
        await update.message.reply_text(
            f'⚙️ *Configuração do Modelo de IA*\n\n'
            f'• *Modelo atual:* `{current}`\n\n'
            f'Escolha o modo de operação desejado:',
            reply_markup=mode_keyboard(),
            parse_mode='Markdown'
        )
        return
    try:
        mode = context.args[0].lower()
        db.set_mode(mode)
        await say(update, f'✅ Modelo configurado para: {mode}.')
    except ValueError as exc:
        await say(update, str(exc))


async def mode_callback(update, context):
    query = update.callback_query
    if not await guard(update): return
    await query.answer()
    try:
        _, mode = query.data.split(':', 1)
        db.set_mode(mode)
        await query.edit_message_text(
            f'✅ *Modelo alterado com sucesso!*\n\n'
            f'• *Novo modo ativo:* `{mode}`',
            reply_markup=mode_keyboard(),
            parse_mode='Markdown'
        )
    except Exception:
        await query.edit_message_text('Não foi possível alterar o modelo. Tente novamente.')


async def backup(update, context):
    if not await guard(update):return
    # backup API SQLite é consistente mesmo com transações WAL.
    with tempfile.TemporaryDirectory() as tmp:
        outfile=Path(tmp)/('financeai-backup-'+db.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
        original = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(outfile)
        try:
            original.backup(dst)
        finally:
            original.close()
            dst.close()
        with outfile.open('rb') as f:
            await update.message.reply_document(f,filename=outfile.name,caption='Backup completo: mantenha este arquivo privado. Faça cópias fora do celular.')


async def chat(update, context):
    if not await guard(update): return
    prompt = (update.message.text or '').strip()
    if not prompt: return
    if len(prompt) > 3000:
        await say(update, 'Mensagem longa demais (máximo 3000 caracteres).')
        return

    # Os botões são atalhos; o uso normal continua sendo texto livre.
    shortcut = normalized(prompt)
    if shortcut == normalized('💰 Saldo'):
        await balance(update, context); return
    if shortcut == normalized('➕ Registrar receita'):
        await say(update, 'Conte normalmente o que recebeu. Exemplo: “Recebi R$ 1.870 de salário”. Vou mostrar a confirmação antes de salvar.'); return
    if shortcut == normalized('➖ Registrar despesa'):
        await say(update, 'Conte normalmente o que gastou. Exemplo: “Gastei R$ 45,90 no almoço”. Vou mostrar a confirmação antes de salvar.'); return
    if shortcut == normalized('📅 Contas a pagar'):
        await debt_list(update, context); return
    if shortcut == normalized('📊 Relatório'):
        await report(update, context); return
    if shortcut == normalized('⚙️ Modelo de IA'):
        await mode_set(update, context); return

    user_id = update.effective_user.id
    drafts = context.application.bot_data.setdefault('natural_drafts', {})
    previous = drafts.get(user_id)
    # Rascunhos de esclarecimento expiram após 3 minutos ou quando o usuário cancelar
    if previous and db.now().timestamp() - previous.get('updated_at', 0) > 180:
        drafts.pop(user_id, None)
        previous = None
    if previous and shortcut in {'cancelar', 'cancela', 'deixa pra la', 'esquece', 'sair', 'parar', 'nao', 'limpar'}:
        drafts.pop(user_id, None)
        await say(update, 'Tudo bem, cancelei a operação pendente. Nenhum registro foi feito.')
        return

    await update.message.chat.send_action('typing')
    try:
        # Perguntas gerais vão direto ao chat. Mensagens sobre dinheiro passam pelo interpretador.
        if not previous and not looks_financial(prompt):
            result_text = await ai.answer(prompt)
            await say(update, result_text)
            return

        result = await ai.interpret_message(prompt, previous)
        intent = result.get('type', 'chat')
        if intent == 'transaction':
            await prepare_natural_transaction(update, context, result, prompt, previous)
            return
        if intent == 'debt':
            await prepare_natural_debt(update, context, result, prompt, previous)
            return
        if intent == 'debt_payment':
            await prepare_natural_debt_payment(update, context, result, prompt, previous)
            return
        if intent == 'reminder':
            await prepare_natural_reminder(update, context, result, prompt, previous)
            return
        if intent == 'clarify':
            if previous:
                # Se já havia um pedido de esclarecimento anterior e não foi resolvido,
                # não prende o usuário em loop infinito: descarta o rascunho e responde via IA conversacional.
                drafts.pop(user_id, None)
                result_text = await ai.answer(prompt)
                await say(update, result_text)
                return
            question = str(result.get('question') or '').strip() or 'Pode explicar um pouco melhor o que você quer registrar?'
            await store_clarification(update, context, prompt, result, question, previous)
            return
        if intent == 'query' and await dispatch_natural_query(update, context, result.get('query')):
            drafts.pop(user_id, None)
            return

        drafts.pop(user_id, None)
        result_text = await ai.answer(prompt)
        await say(update, result_text)
    except RuntimeError as exc:
        log.warning('Falha na IA: %s', type(exc).__name__)
        await say(update, 'Não consegui interpretar sua mensagem pela NVIDIA: ' + str(exc))
    except Exception:
        log.exception('Erro inesperado sem registrar a mensagem')
        await say(update, 'Ocorreu um erro inesperado. Nenhum lançamento foi feito. Tente novamente ou verifique o log do Termux.')


async def prepare_natural_transaction(update, context, result, prompt, previous=None):
    """Valida uma movimentação interpretada e pede confirmação antes de salvar."""
    user_id = update.effective_user.id
    kind = str(result.get('kind') or '').lower().strip()
    amount_raw = result.get('amount')
    category = str(result.get('category') or '').strip()
    description = str(result.get('description') or '').strip()
    occurred_date = result.get('occurred_date')

    if kind not in ('receita', 'despesa'):
        await store_clarification(update, context, prompt, result,
            'Você está falando de dinheiro que recebeu (receita) ou que gastou/pagou (despesa)?', previous)
        return
    if amount_raw in (None, ''):
        await store_clarification(update, context, prompt, result,
            'Qual foi o valor exato dessa movimentação?', previous)
        return
    if not category and not description:
        await store_clarification(update, context, prompt, result,
            'Com o que foi essa movimentação? Por exemplo: almoço, salário ou transporte.', previous)
        return

    try:
        amount = db.money_to_cents(amount_raw)
        if occurred_date:
            date.fromisoformat(str(occurred_date))
    except (ValueError, TypeError):
        await store_clarification(update, context, prompt, result,
            'Não consegui validar o valor ou a data. Informe o valor em reais, por exemplo R$ 45,90.', previous)
        return

    if not category:
        category = 'Outros'
    if not description:
        description = category
    action = {
        'type': 'transaction', 'kind': kind, 'amount': amount,
        'category': category, 'description': description,
        'occurred_date': str(occurred_date) if occurred_date else None,
    }
    context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
    date_line = f'\nData: {db.short_date(str(occurred_date))}' if occurred_date else ''
    await ask_action_confirmation(update, context, action,
        f'Confira antes de registrar:\n\nTipo: {kind.capitalize()}\nValor: {db.brl(amount)}\nCategoria: {category}\nDescrição: {description}{date_line}\n\nPosso salvar no seu histórico financeiro?')


async def prepare_natural_debt(update, context, result, prompt, previous=None):
    """Prepara uma conta a pagar sem confundi-la com uma despesa já paga."""
    user_id = update.effective_user.id
    amount_raw = result.get('amount')
    title = str(result.get('title') or result.get('description') or '').strip()
    due_date = result.get('due_date')
    if amount_raw in (None, ''):
        await store_clarification(update, context, prompt, result,
            'Qual é o valor dessa conta?', previous)
        return
    if not title:
        await store_clarification(update, context, prompt, result,
            'Qual conta devo adicionar? Por exemplo, aluguel ou conta de luz.', previous)
        return
    if not due_date:
        await store_clarification(update, context, prompt, result,
            f'Qual é a data de vencimento de “{title}”? Informe dia, mês e ano.', previous)
        return
    try:
        amount = db.money_to_cents(amount_raw)
        due_date = date.fromisoformat(str(due_date)).isoformat()
    except (ValueError, TypeError):
        await store_clarification(update, context, prompt, result,
            'Não consegui validar o valor ou o vencimento. Informe o valor e a data da conta.', previous)
        return

    action = {'type': 'debt', 'amount': amount, 'due_date': due_date, 'title': title}
    context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
    await ask_action_confirmation(update, context, action,
        f'Confira a conta antes de salvar:\n\nConta: {title}\nValor: {db.brl(amount)}\nVencimento: {db.short_date(due_date)}\n\nPosso adicionar à sua lista de contas a pagar?')


async def prepare_natural_debt_payment(update, context, result, prompt, previous=None):
    """Relaciona um pagamento apenas a uma dívida aberta identificada sem ambiguidade."""
    user_id = update.effective_user.id
    title = str(result.get('title') or result.get('description') or '').strip()
    debts = db.get_debts()
    if not debts:
        await store_clarification(update, context, prompt, result,
            'Não há contas em aberto cadastradas para relacionar esse pagamento. Você quer registrar como uma despesa comum? Diga, por exemplo, “sim, registre como despesa de aluguel”.', previous)
        return
    if not title:
        choices = ', '.join(d['title'] for d in debts[:8])
        await store_clarification(update, context, prompt, result,
            'Qual conta em aberto você pagou? Contas cadastradas: ' + choices, previous)
        return

    target = normalized(title)
    exact = [d for d in debts if normalized(d['title']) == target]
    matches = exact or [d for d in debts if target in normalized(d['title']) or normalized(d['title']) in target]
    if len(matches) != 1:
        choices = ', '.join(d['title'] for d in debts[:8])
        question = (f'Encontrei mais de uma conta que pode ser “{title}”. Qual delas você pagou? ' if matches
                    else f'Não encontrei uma conta em aberto chamada “{title}”. Qual conta cadastrada você pagou? ')
        await store_clarification(update, context, prompt, result, question + choices, previous)
        return

    debt = matches[0]
    remaining = debt['original_amount'] - debt['paid_amount']
    amount_raw = result.get('amount')
    if result.get('full_payment') is True and amount_raw in (None, ''):
        amount = remaining
    elif amount_raw in (None, ''):
        await store_clarification(update, context, prompt, result,
            f'Quanto você pagou da conta “{debt["title"]}”? Ainda faltam {db.brl(remaining)}.', previous)
        return
    else:
        try:
            amount = db.money_to_cents(amount_raw)
        except (ValueError, TypeError):
            await store_clarification(update, context, prompt, result,
                'Qual foi o valor pago? Informe em reais, por exemplo R$ 100,00.', previous)
            return
    if amount > remaining:
        await store_clarification(update, context, prompt, result,
            f'O valor informado é maior que o restante de {db.brl(remaining)}. Quanto você pagou dessa conta?', previous)
        return

    action = {'type': 'debt_payment', 'debt_id': debt['id'], 'amount': amount, 'title': debt['title']}
    context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
    await ask_action_confirmation(update, context, action,
        f'Confira o pagamento que será registrado:\n\nConta: {debt["title"]}\nValor pago: {db.brl(amount)}\nRestante depois do registro: {db.brl(remaining - amount)}\n\nPosso registrar esse pagamento? Isso não transfere dinheiro de verdade.')


async def prepare_natural_reminder(update, context, result, prompt, previous=None):
    """Prepara um lembrete com data/hora futura e pede confirmação."""
    user_id = update.effective_user.id
    content = str(result.get('title') or result.get('description') or '').strip()
    reminder_at = result.get('reminder_at')
    if not content:
        await store_clarification(update, context, prompt, result,
            'Sobre o que você quer ser lembrado?', previous)
        return
    if not reminder_at:
        await store_clarification(update, context, prompt, result,
            'Em qual dia e horário devo lembrar você? Informe a data e a hora.', previous)
        return
    try:
        parsed = datetime.fromisoformat(str(reminder_at))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        else:
            parsed = parsed.astimezone(TZ)
        if parsed <= db.now():
            await store_clarification(update, context, prompt, result,
                'Essa data e hora já passaram. Qual data e horário futuros você prefere?', previous)
            return
    except (ValueError, TypeError):
        await store_clarification(update, context, prompt, result,
            'Não consegui entender a data e hora. Informe uma data e horário futuros, por favor.', previous)
        return

    due_at = parsed.isoformat()
    action = {'type': 'reminder', 'due_at': due_at, 'content': content}
    context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
    await ask_action_confirmation(update, context, action,
        f'Confira o lembrete:\n\nLembrar: {content}\nQuando: {parsed.strftime("%d/%m/%Y às %H:%M")}\n\nPosso salvar esse lembrete?')


async def dispatch_natural_query(update, context, query):
    """Encaminha consultas interpretadas a rotinas locais verificáveis."""
    # Guardrail: nunca disparar help_cmd se a mensagem contém conteúdo financeiro.
    # O classificador pode falhar — este bloqueio em código é o safety net definitivo.
    prompt = (update.message.text or '') if update.message else ''
    if str(query or '').strip().lower() == 'help' and (
        re.search(r'R\$', prompt) or
        re.search(r'\b(salário|salario|estimado|contrato|prefeitura|receber|complemento|'
                  r'pagar|pagamento|dívida|divida|conta|despesa|receita|saldo|'
                  r'dia\s+\d|^\d+º|útil|fatura|adiantamento|renda|empréstimo)\b', prompt, re.I)
    ):
        # Redireciona para resposta conversacional em vez de exibir o tutorial
        log.info('Guardrail: query help bloqueado para mensagem financeira: %.80s', prompt)
        result_text = await ai.answer(prompt)
        await say(update, result_text)
        return True

    # Guardrail: nunca disparar debt_list (debts) se a mensagem é sobre recebimento ou salário.
    if str(query or '').strip().lower() == 'debts' and (
        re.search(r'\b(receb|sal[aá]rio|complemento|adiantamento|ganh|renda|prefeitura|contrato|a receber)\b', prompt, re.I)
    ):
        log.info('Guardrail: query debts bloqueado para mensagem de recebimento: %.80s', prompt)
        result_text = await ai.answer(prompt)
        await say(update, result_text)
        return True

    handlers = {
        'balance': balance, 'debts': debt_list, 'report': report,
        'history': history, 'plan': plan, 'reminders': reminder_list,
        'help': help_cmd,
    }
    handler = handlers.get(str(query or '').strip().lower())
    if handler is None:
        return False
    await handler(update, context)
    return True


async def natural_action_callback(update, context):
    """Aplica uma ação financeira somente após o botão de confirmação."""
    query = update.callback_query
    if not await guard(update):
        return
    await query.answer()
    try:
        prefix, decision, nonce = query.data.split(':', 2)
    except (AttributeError, ValueError):
        await query.edit_message_text('Confirmação inválida. Nenhum registro foi feito.')
        return
    pending = context.application.bot_data.setdefault('natural_actions', {})
    entry = pending.pop(nonce, None)
    if prefix != 'nl' or decision not in ('confirm', 'cancel') or not entry:
        await query.edit_message_text('Confirmação vencida ou já utilizada. Nenhum registro foi feito.')
        return
    if db.now().timestamp() - entry.get('created_at', 0) > 600:
        await query.edit_message_text('Esta confirmação expirou. Envie a informação novamente; nada foi registrado.')
        return
    if decision == 'cancel':
        await query.edit_message_text('Cancelado. Nenhum registro foi feito.')
        return

    action = entry.get('action') or {}
    try:
        action_type = action.get('type')
        if action_type == 'transaction':
            txid = db.insert_transaction(
                action['kind'], int(action['amount']), action['category'],
                action.get('description', ''), action.get('occurred_date'))
            detail = f'\nData: {db.short_date(action["occurred_date"])}' if action.get('occurred_date') else ''
            await query.edit_message_text(
                f'✅ Lançamento registrado (#{txid}).\n{action["kind"].capitalize()}: {db.brl(action["amount"])}\n'
                f'{action["category"]} — {action.get("description", "")}{detail}\n'
                f'Saldo histórico registrado: {db.brl(db.summary()["total"])}')
        elif action_type == 'debt':
            debt_id = db.add_debt(int(action['amount']), action['due_date'], action['title'])
            await query.edit_message_text(
                f'✅ Conta adicionada (#{debt_id}).\n{action["title"]}: {db.brl(action["amount"])}\n'
                f'Vencimento: {db.short_date(action["due_date"])}')
        elif action_type == 'debt_payment':
            remaining = db.pay_debt(int(action['debt_id']), int(action['amount']))
            await query.edit_message_text(
                f'✅ Pagamento registrado para “{action["title"]}”: {db.brl(action["amount"])}.\n'
                f'Restante: {db.brl(remaining)}\nSaldo histórico registrado: {db.brl(db.summary()["total"])}\n'
                'Isso é apenas um registro no FinanceAI; não houve transferência bancária.')
        elif action_type == 'tx_delete':
            db.delete_transaction(int(action['tx_id']))
            await query.edit_message_text(
                f'🗑️ Lançamento #{action["tx_id"]} apagado.\nSaldo histórico registrado: {db.brl(db.summary()["total"])}')
        elif action_type == 'reminder':
            reminder_id = db.insert_reminder(action['due_at'], action['content'])
            reminder_time = datetime.fromisoformat(action['due_at']).astimezone(TZ)
            await query.edit_message_text(
                f'✅ Lembrete salvo (#{reminder_id}).\n{action["content"]}\n'
                f'Quando: {reminder_time.strftime("%d/%m/%Y às %H:%M")}')
        else:
            await query.edit_message_text('Ação não reconhecida. Nenhum registro foi feito.')
    except (ValueError, KeyError, TypeError) as exc:
        log.warning('Ação natural não registrada: %s', type(exc).__name__)
        await query.edit_message_text('Não consegui validar os dados; nenhum registro foi feito. Envie a mensagem novamente.')
    except Exception:
        log.exception('Erro ao confirmar ação natural')
        await query.edit_message_text('Ocorreu um erro ao registrar; confira o histórico antes de tentar novamente.')


async def reminder_worker(application):
    while True:
        try:
            if OWNER_ID:
                db.generate_recurring()
                # Lembretes manuais cadastrados pelo usuário
                for reminder in db.due_reminders():
                    try:
                        await application.bot.send_message(
                            OWNER_ID,
                            f'⏰ *Lembrete #{reminder["id"]}*\n{reminder["content"]}',
                            parse_mode='Markdown')
                        db.mark_reminder(reminder['id'])
                    except Exception as exc:
                        log.warning('Falha de envio do lembrete #%s: %s', reminder['id'], type(exc).__name__)

                # Alertas automáticos de dívidas próximas ou em atraso
                for debt in db.get_debts_due_soon():
                    if db.was_debt_notified(debt['id'], debt['alert_type']):
                        continue
                    days = debt['days_left']
                    rem = db.brl(debt['remaining'])
                    due_br = db.short_date(debt['due_date'])
                    if days == 3:
                        msg = f'📅 *Conta vencendo em 3 dias\\!*\n📌 {debt["title"]}\n💳 Restante: {rem}\n🗓 Vencimento: {due_br}'
                    elif days == 1:
                        msg = f'⚠️ *Conta vence amanhã\\!*\n📌 {debt["title"]}\n💳 Restante: {rem}\n🗓 Vencimento: {due_br}'
                    elif days == 0:
                        msg = f'🔴 *Conta vence HOJE\\!*\n📌 {debt["title"]}\n💳 Restante: {rem}\n🗓 Vencimento: {due_br}'
                    else:
                        msg = (f'❗ *Conta em atraso\\!*\n📌 {debt["title"]}\n💳 Restante: {rem}\n'
                               f'🗓 Venceu em: {due_br} ({abs(days)} dia{"s" if abs(days) > 1 else ""} atrás)')
                    try:
                        await application.bot.send_message(OWNER_ID, msg, parse_mode='MarkdownV2')
                        db.mark_debt_notified(debt['id'], debt['alert_type'])
                    except Exception as exc:
                        log.warning('Falha ao enviar alerta de dívida #%s: %s', debt['id'], type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Falha no verificador de lembretes')
        await asyncio.sleep(30)


async def on_init(app):
    app.bot_data['reminder_task']=asyncio.create_task(reminder_worker(app))
    log.info('FinanceAI inicializado. Proprietário configurado: %s',bool(OWNER_ID))


async def on_stop(app):
    task=app.bot_data.get('reminder_task')
    if task:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)


def main():
    global LOCK
    if not TOKEN:raise SystemExit('Configure TELEGRAM_BOT_TOKEN no arquivo .env.')
    BASE.joinpath('logs').mkdir(exist_ok=True)
    LOCK=open(BASE/'data'/'financeai.lock','w') if (BASE/'data').is_dir() else None
    if LOCK is None:
        (BASE/'data').mkdir(parents=True,exist_ok=True)
        LOCK=open(BASE/'data'/'financeai.lock','w')
    if fcntl:
        try:
            fcntl.flock(LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Outra instância do bot já está em execução.') from None
    else:
        try:
            import msvcrt
            msvcrt.locking(LOCK.fileno(), msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError):
            pass
    db.init()
    app=(ApplicationBuilder().token(TOKEN).post_init(on_init).post_stop(on_stop).build())
    routes={'id':show_id,'start':start,'ajuda':help_cmd,'receita':movement,'despesa':movement,
            'saldo':balance,'conta':debt_add,'dividas':debt_list,'pagar':pay,'historico':history,
            'relatorio':report,'planejar':plan,'lembrete':reminder_add,'lembretes':reminder_list,
            'modelo':mode_set,'backup':backup,'corrigir_conta':debt_edit,'excluir_conta':debt_delete,'apagar':tx_delete,'fixa':recurring_add}
    for name, handler in routes.items():
        app.add_handler(CommandHandler(name,handler))
    app.add_handler(CallbackQueryHandler(payment_callback,pattern='^(pay|cancel):[a-f0-9]{16}$'))
    app.add_handler(CallbackQueryHandler(natural_action_callback,pattern='^nl:(confirm|cancel):[a-f0-9]{16}$'))
    app.add_handler(CallbackQueryHandler(mode_callback,pattern='^mode:(auto|rapido|avancado)$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,chat))
    log.info('Iniciando o bot em long polling...')
    app.run_polling(drop_pending_updates=False)


if __name__=='__main__':main()
