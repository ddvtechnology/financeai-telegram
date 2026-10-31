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
from logging.handlers import RotatingFileHandler
import re
import secrets
import sqlite3
import tempfile
import time
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup)
from telegram.constants import ChatType
from telegram.error import NetworkError
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters)
import ai
import battery
import db
import importar
import quick
import when
from config import (BASE, BATTERY_ALERTS, BATTERY_FULL_ALERT, BATTERY_LEVELS, MORNING_HOUR,
                    MODELS, OWNER_ID, TOKEN, TZ, TZ_NAME)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
# httpx registra a URL completa das requisições, que contém o token do bot: mantém só avisos/erros.
for _noisy in ('httpx', 'httpcore'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger('financeai')
LOCK = None


def setup_file_logging(log_dir=None):
    """Configura rotação automática do log em arquivo para não encher o armazenamento do celular."""
    target = Path(log_dir or (BASE / 'logs'))
    target.mkdir(parents=True, exist_ok=True)
    log_file = (target / 'bot.log').resolve()
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and Path(getattr(h, 'baseFilename', '')).resolve() == log_file:
            return log_file
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    root.addHandler(handler)
    return log_file

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


async def safe_answer(query):
    """Confirma o toque no botão. Se a rede falhar aqui, a ação do botão ainda deve ser executada."""
    try:
        await query.answer()
    except NetworkError as exc:
        log.warning('Não consegui confirmar o toque no botão (rede): %s', exc)


async def on_error(update, context):
    """Falha de rede é rotina em celular: um aviso curto. Outros erros mantêm o traceback."""
    err = context.error
    if isinstance(err, NetworkError):  # inclui TimedOut
        log.warning('Falha de rede com o Telegram: %s', str(err)[:150])
        return
    log.error('Erro não tratado', exc_info=err)


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


def _msg(update):
    return getattr(update, 'message', None) or (
        update.callback_query.message if getattr(update, 'callback_query', None) else None
    )


async def say(update, text, keyboard=None, parse_mode='Markdown'):
    """Divide respostas mantendo o teclado persistente e suportando formatação Markdown/HTML."""
    msg = _msg(update)
    markup = keyboard if keyboard is not None else main_keyboard()
    if parse_mode == 'Markdown':
        text = clean_telegram_markdown(text)
    chunks = [text[start:start+3900] for start in range(0, len(text), 3900)] or [text]
    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        k = markup if is_last else None
        try:
            await msg.reply_text(chunk, reply_markup=k, parse_mode=parse_mode)
        except Exception:
            # Fallback se a mensagem tiver formatação inválida
            await msg.reply_text(chunk, reply_markup=k)


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton('💰 Saldo'), KeyboardButton('📅 Contas a pagar')],
            [KeyboardButton('➕ Registrar receita'), KeyboardButton('➖ Registrar despesa')],
            [KeyboardButton('📊 Relatório'), KeyboardButton('📜 Histórico')],
            [KeyboardButton('🔁 Contas fixas'), KeyboardButton('⏰ Lembretes')],
            [KeyboardButton('🧭 Planejamento'), KeyboardButton('🛠️ Menu e Opções')],
        ], resize_keyboard=True, is_persistent=True,
        input_field_placeholder='Converse ou toque em um botão abaixo...'
    )


def normalized(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def asks_battery(text):
    """Pergunta curta sobre a bateria do celular (ex.: 'qual a bateria?'), sem cara de gasto."""
    words = normalized(text).split()
    return 'bateria' in words and len(words) <= 6 and not looks_financial(text)


def looks_financial(text):
    plain = normalized(text)
    return bool(
        re.search(r'\b(gastei|gasto|despesa|comprei|paguei|pagamento|quitei|recebi|ganhei|salario|renda|receita|conta a pagar|contas a pagar|vencimento|vence|divida|dividas|saldo|relatorio|historico|planejamento|orcamento|lembrete|lembr\w*|avis\w*|alarme|notifi\w*|boleto|fatura|cartao|aluguel|supermercado|mercado|alimentacao|transporte|juros|financiamento)\b', plain)
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
    await _msg(update).reply_text(message, reply_markup=keyboard)


async def show_id(update, context):
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await _msg(update).reply_text(f'Seu ID numérico Telegram é: {update.effective_user.id}\nGuarde-o para configurar TELEGRAM_OWNER_ID no .env.')


async def start(update, context):
    if not await guard(update): return
    await _msg(update).reply_text('FinanceAI está pronto. Vamos cuidar das suas finanças conversando normalmente.\n\n' + HELP, reply_markup=main_keyboard())


async def help_cmd(update, context):
    if not await guard(update): return
    await _msg(update).reply_text(HELP, reply_markup=main_keyboard())


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
    m = re.match(r'^/(?:apagar|estornar)(?:@\w+)?\s+(\d+)\s*$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /apagar <id do lançamento> (ou /estornar <id>)\nVeja os ids em /historico.'); return
    tx = db.get_transaction(int(m.group(1)))
    if not tx:
        await say(update, 'Lançamento não encontrado.'); return
    if tx['debt_id']:
        action = {'type': 'tx_refund_debt', 'tx_id': tx['id']}
        await ask_action_confirmation(update, context, action,
            f'Estornar este pagamento de dívida?\n\n#{tx["id"]} Pagamento de {db.brl(tx["amount"])}\n'
            f'Conta: {tx["description"]} (Dívida #{tx["debt_id"]})\nData: {db.short_date(tx["occurred_at"])}\n\n'
            'O valor voltará a ficar em aberto na conta e o lançamento será removido.')
        return
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


async def recurring_list(update, context):
    if not await guard(update): return
    rows = db.get_recurring()
    if not rows:
        await say(update, 'Não há contas fixas cadastradas.\nVocê pode dizer, por exemplo: “Cadastrar conta fixa de 100 reais todo dia 4 para Academia”.'); return
    w_names = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo')
    lines = ['🔁 *CONTAS FIXAS CADASTRADAS:*\n']
    buttons = []
    for r in rows:
        when_str = f"toda {w_names[r['weekday']]}" if r.get('weekday') is not None else f"todo dia {r['day']:02d}"
        lines.append(f'• *#{r["id"]} {r["title"]}* — {db.brl(r["amount"])} ({when_str})')
        buttons.append([
            InlineKeyboardButton(f'✏️ Valor #{r["id"]} ({r["title"][:14]})', callback_data=f'mn:recedit:{r["id"]}'),
            InlineKeyboardButton(f'🗑️ Excluir #{r["id"]}', callback_data=f'mn:recdel:{r["id"]}'),
        ])
    await say(update, '\n'.join(lines), keyboard=InlineKeyboardMarkup(buttons) if buttons else None)


async def recurring_delete(update, context):
    if not await guard(update): return
    m = re.match(r'^/excluir_fixa(?:@\w+)?\s+(\d+)\s*$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /excluir_fixa <id>\nVeja os ids em /fixas.'); return
    rec_id = int(m.group(1))
    rec = next((r for r in db.get_recurring(active_only=False) if r['id'] == rec_id), None)
    if not rec:
        await say(update, f'Conta fixa #{rec_id} não encontrada.'); return
    action = {'type': 'recurring_delete', 'rec_id': rec_id, 'title': rec['title']}
    await ask_action_confirmation(update, context, action,
        f'Excluir a conta fixa #{rec_id} ({rec["title"]} — {db.brl(rec["amount"])})?\n'
        'Ela deixará de gerar novas contas nos próximos meses.')


async def balance(update, context):
    if not await guard(update): return
    s = db.summary()
    year, month_num = s["month"].split('-')
    month_br = f'{month_num}/{year}'
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('📅 Contas a pagar', callback_data='mn:debts'),
         InlineKeyboardButton('📜 Histórico', callback_data='mn:hist:all')],
        [InlineKeyboardButton('📊 Relatório do mês', callback_data='mn:report'),
         InlineKeyboardButton('🧭 Planejamento', callback_data='mn:plan')],
    ])
    await say(update,
        f'💰 *Saldo Atual Registrado:* {db.brl(s["total"])}\n\n'
        f'📅 *Mês de Referência:* {month_br}\n'
        f'🟢 *Receitas:* {db.brl(s["receita"])}\n'
        f'🔴 *Despesas:* {db.brl(s["despesa"])}',
        keyboard=kb
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


async def debt_list(update, context, month=None):
    if not await guard(update): return
    all_debts = db.get_debts()
    if not all_debts:
        await say(update, '🎉 Parabéns! Nenhuma conta ou dívida em aberto no momento.')
        return
    months = db.get_debt_months()
    active_month = None if month in (None, 'all') else month
    debts = db.get_debts(month=active_month) if active_month else all_debts
    if not debts and active_month:
        debts = all_debts
        active_month = None

    total_all = sum(d['original_amount'] - d['paid_amount'] for d in all_debts)
    total_view = sum(d['original_amount'] - d['paid_amount'] for d in debts)
    head_label = f'{active_month[5:]}/{active_month[:4]}' if active_month else 'TODAS'
    lines = [f'📅 <b>CONTAS E DÍVIDAS EM ABERTO ({head_label}):</b>\n']
    for d in debts[:40]:
        rem = d["original_amount"] - d["paid_amount"]
        due_br = db.short_date(d["due_date"])
        title = html.escape(str(d["title"]))
        lines.append(f'📌 <b>#{d["id"]} {title}</b>\n   Restante: <code>{html.escape(db.brl(rem))}</code> | 🗓 Vencimento: <code>{due_br}</code>\n')
    if len(debts) > 40:
        lines.append(f'... e mais {len(debts) - 40} contas.\n')
    if active_month:
        lines.append(f'📆 <b>Subtotal de {head_label}:</b> <code>{html.escape(db.brl(total_view))}</code>')
    lines.append(f'💳 <b>Total geral em aberto:</b> <code>{html.escape(db.brl(total_all))}</code>')

    filter_row = []
    for m_str in months[:3]:
        m_br = f'{m_str[5:]}/{m_str[:4]}'
        mark = '🔘 ' if active_month == m_str else '📅 '
        filter_row.append(InlineKeyboardButton(f'{mark}{m_br}', callback_data=f'mn:debts:{m_str}'))
    if len(months) > 1:
        mark_all = '🔘 ' if active_month is None else '📋 '
        filter_row.append(InlineKeyboardButton(f'{mark_all}Todas', callback_data='mn:debts:all'))

    rows_kb = []
    if filter_row:
        rows_kb.append(filter_row[:2])
        if len(filter_row) > 2:
            rows_kb.append(filter_row[2:])
    rows_kb.extend([
        [InlineKeyboardButton('💳 Registrar pagamento de conta', callback_data='mn:paylist:0')],
        [InlineKeyboardButton('✏️ Editar data/valor', callback_data='mn:editdebt_list:0'),
         InlineKeyboardButton('🗑️ Excluir conta', callback_data='mn:deldebt_list:0')],
        [InlineKeyboardButton('🔁 Contas fixas', callback_data='mn:rec_list')],
    ])
    await say(update, '\n'.join(lines), keyboard=InlineKeyboardMarkup(rows_kb), parse_mode='HTML')


async def debt_edit(update, context):
    if not await guard(update): return
    text = update.message.text.strip()
    # Aceita # opcional no ID e ignora texto/nome da conta se o usuário escrever (ex.: /corrigir_conta #27 Combustível 20,00)
    m_head = re.match(r'^/corrigir_conta(?:@\w+)?\s+#?(\d+)\s+(.+)$', text)
    if not m_head:
        await say(update, 'Formato:\n• Só valor: /corrigir_conta 27 20,00\n• Só data: /corrigir_conta 27 15/11/2026\n• Valor e data: /corrigir_conta 27 20,00 15/11/2026\nOu use o botão ✏️ Editar data/valor em 📅 Contas a pagar.')
        return
    try:
        debt_id = int(m_head.group(1))
        rest = m_head.group(2).strip()
        due_iso = None
        m_dt = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', rest)
        if m_dt:
            due_iso = db.parse_date(m_dt.group(1))
            rest = (rest[:m_dt.start()] + ' ' + rest[m_dt.end():]).strip()
        amount = None
        # Procura o último número monetário no restante do texto (ignorando eventual nome da conta antes)
        tokens = [t for t in rest.split() if re.search(r'\d', t)]
        if tokens:
            amount = db.money_to_cents(tokens[-1])
        if amount is None and due_iso is None:
            raise ValueError('Informe o novo valor (ex.: 20,00) e/ou a nova data (ex.: 15/11/2026).')

        with db.conn() as c:
            d = c.execute('SELECT * FROM debts WHERE id=?', (debt_id,)).fetchone()
        if not d:
            raise ValueError(f'Dívida #{debt_id} não encontrada.')

        new_total = (d['paid_amount'] + amount) if amount is not None else d['original_amount']
        remaining = db.update_debt(debt_id, new_total, new_due_date=due_iso)
        final_due = db.short_date(due_iso or d['due_date'])
        await say(update, f'✅ Conta #{debt_id} ({d["title"]}) atualizada!\n• Valor restante: {db.brl(remaining)}\n• Vencimento: {final_due}')
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
    await safe_answer(q)
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


def asks_history(text):
    """'últimas despesas/receitas/movimentações' → lista real do banco (com datas), nunca resposta da IA."""
    plain = normalized(text)
    m = re.fullmatch(r'(?:quais (?:sao|foram) )?(?:as |os |meus |minhas )*(?:ultimas?|ultimos?|recentes?) (?:\w+ )?'
                     r'(despesas?|gastos?|receitas?|entradas?|movimentacoes|lancamentos|transacoes)'
                     r'(?: registrad[oa]s?| recentes?)?(?: com (?:as )?datas?)?', plain)
    if not m:
        return None
    word = m.group(1)
    if word.startswith(('despesa', 'gasto')):
        return 'despesa'
    if word.startswith(('receita', 'entrada')):
        return 'receita'
    return 'todas'


async def history(update, context, kind=None):
    if not await guard(update):return
    transactions=db.get_transactions(60 if kind else 20)
    if kind:transactions=[t for t in transactions if t['kind']==kind][:20]
    if not transactions:await say(update,'Não há movimentações registradas.');return
    lines=['ÚLTIMAS MOVIMENTAÇÕES:' if not kind else f'ÚLTIMAS {"DESPESAS" if kind=="despesa" else "RECEITAS"}:']
    for t in transactions:
        emoji='➕' if t['kind']=='receita' else '➖'
        lines.append(f'{emoji} #{t["id"]} {db.short_date(t["occurred_at"])} {db.brl(t["amount"])} | {t["category"]} | {t["description"]}\n')
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('📋 Todas', callback_data='mn:hist:all'),
         InlineKeyboardButton('🟢 Receitas', callback_data='mn:hist:receita'),
         InlineKeyboardButton('🔴 Despesas', callback_data='mn:hist:despesa')],
        [InlineKeyboardButton('🗑️ Apagar ou Estornar lançamento', callback_data='mn:txdel_list')],
    ])
    await say(update, '\n'.join(lines), keyboard=kb)


async def report(update, context, month=None):
    if not await guard(update): return
    month = month or (context.args[0] if getattr(context, 'args', None) else db.now().strftime('%Y-%m'))
    try:
        text = month_report_text(month)
    except ValueError as exc:
        await say(update, str(exc))
        return

    month_br = f'{month[5:]}/{month[:4]}'
    prev_m = db.previous_month(date.fromisoformat(month + '-01'))
    next_m = db.next_month(month)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f'⬅️ {prev_m[5:]}/{prev_m[:4]}', callback_data=f'mn:rep:{prev_m}'),
         InlineKeyboardButton(f'{next_m[5:]}/{next_m[:4]} ➡️', callback_data=f'mn:rep:{next_m}')],
        [InlineKeyboardButton(f'📄 Baixar Planilha CSV ({month_br})', callback_data=f'mn:repcsv:{month}')],
    ])
    await say(update, text, keyboard=kb)

    doc = build_csv(month)
    if doc:
        await _msg(update).reply_document(doc, filename=doc.name, caption=f'📄 Extrato detalhado em planilha ({month_br})')


async def plan(update, context, month=None):
    if not await guard(update): return
    p = db.planning()
    debt_months = db.get_debt_months()
    target_m = month or (p['month'] if p['due_remaining_month'] > 0 else (debt_months[0] if debt_months else p['month']))
    debts_m = db.get_debts(month=target_m)
    q1_debts = [d for d in debts_m if int(d['due_date'][8:10]) <= 15]
    q2_debts = [d for d in debts_m if int(d['due_date'][8:10]) > 15]

    def _monthly_flow_amt(d):
        rem = d['original_amount'] - d['paid_amount']
        # Perfumes e Roupas são abatidos R$ 100/mês no dia 30
        if normalized(d['title']) in ('perfumes', 'roupas ads multimarcas') and rem > 10000:
            return 10000
        return rem

    q1_total = sum(_monthly_flow_amt(d) for d in q1_debts)
    q2_total = sum(_monthly_flow_amt(d) for d in q2_debts)
    flow_total = q1_total + q2_total
    raw_total = sum(d['original_amount'] - d['paid_amount'] for d in debts_m)
    m_br = f'{target_m[5:]}/{target_m[:4]}'

    # Receitas fixas mensais cadastradas (R$ 70 dia 10 + R$ 935 dia 15 + R$ 830 dia 30 = R$ 1.835,00)
    expected_income_m = 183500
    projected_end = p['balance'] + expected_income_m - flow_total

    lines = [
        f'🧭 *PLANEJAMENTO FINANCEIRO ({p["month"]})*\n',
        f'💰 *Saldo atual em caixa:* `{db.brl(p["balance"])}`',
        f'🟢 *Recebido em {p["month"][5:]}/{p["month"][:4]}:* `{db.brl(p["income_month"])}`',
        f'🔴 *Gasto em {p["month"][5:]}/{p["month"][:4]}:* `{db.brl(p["spent_month"])}`',
        f'📅 *Dívidas ainda a vencer neste mês:* `{db.brl(p["due_remaining_month"])}`',
        f'📅 *Dívidas a vencer no próximo mês:* `{db.brl(p["due_next_month"])}`\n',
        f'🟢 *Receitas Fixas Previstas em {m_br}:* `{db.brl(expected_income_m)}`',
        '  • Até dia 10: `R$ 70,00` (Sistema Joaninha)',
        '  • Dia 15: `R$ 935,00` (Salário 1ª parcela)',
        '  • Dia 30: `R$ 830,00` (Salário 2ª parcela)\n',
        f'📊 *Detalhamento de Contas — {m_br}:*',
        f'• *1ª Quinzena (dias 01 a 15):* `{db.brl(q1_total)}` ({len(q1_debts)} contas)',
        f'• *2ª Quinzena (dias 16 a 30):* `{db.brl(q2_total)}` ({len(q2_debts)} contas)',
        f'• *Desembolso previsto no mês ({m_br}):* `{db.brl(flow_total)}`',
    ]
    if raw_total != flow_total:
        lines.append(f'  *(Saldo total das fichas em {m_br}: {db.brl(raw_total)}, considerando R$ 100/mês em Perfumes e Roupas)*')
    lines.append(f'\n✅ *Saldo projetado ao final de {m_br}:* `{db.brl(projected_end)}`'
                 f'\n  *(Caixa atual {db.brl(p["balance"])} + Receitas {db.brl(expected_income_m)} − Contas {db.brl(flow_total)})*')

    m_buttons = [
        InlineKeyboardButton(f'{"🔘 " if m == target_m else "📅 "}{m[5:]}/{m[:4]}', callback_data=f'mn:plan:{m}')
        for m in debt_months[:3]
    ]
    kb_rows = []
    if m_buttons:
        kb_rows.append(m_buttons)
    kb_rows.append([InlineKeyboardButton(f'📅 Ver contas de {m_br}', callback_data=f'mn:debts:{target_m}')])
    await say(update, '\n'.join(lines), keyboard=InlineKeyboardMarkup(kb_rows))


async def clock_cmd(update, context):
    if not await guard(update): return
    n = db.now()
    off = n.strftime('%z')
    lines = [f'🕒 Agora no bot: {ai.WEEKDAYS[n.weekday()]}, {n.strftime("%d/%m/%Y %H:%M:%S")}',
             f'🌎 Fuso: {TZ_NAME} (UTC{off[:3]}:{off[3:]})',
             'Se estiver diferente do relógio do celular, ative a data e hora automáticas do Android.']
    pending = db.get_reminders()[:5]
    if pending:
        lines.append('\n⏰ Próximos lembretes:')
        for r in pending:
            due = datetime.fromisoformat(r['due_at']).astimezone(TZ)
            lines.append(f'• {due.strftime("%d/%m %H:%M")} — {r["content"]}')
    await say(update, '\n'.join(lines), parse_mode=None)


async def battery_cmd(update, context):
    if not await guard(update): return
    await _msg(update).reply_text('🔋 Lendo a bateria...')
    info, reason = await battery.check()  # a 1ª chamada pode demorar para acordar o app
    if info is None:
        log.info('Leitura de bateria falhou: %s', reason)
        await say(update, 'Não consegui ler a bateria.\n\n' + battery.explain(reason), parse_mode=None)
        return
    await say(update, battery.describe(info), parse_mode=None)


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
    if not rows:await say(update,'Não há lembretes pendentes.\nVocê pode criar um dizendo, por exemplo: “Me lembra amanhã às 9h de pagar aluguel”.');return
    lines = ['⏰ *LEMBRETES PENDENTES:*\n']
    buttons = []
    for r in rows:
        due_str = datetime.fromisoformat(r["due_at"]).astimezone(TZ).strftime("%d/%m/%Y %H:%M")
        lines.append(f'• *#{r["id"]}* {due_str} — {r["content"]}')
        buttons.append([InlineKeyboardButton(f'🗑️ Cancelar #{r["id"]} ({r["content"][:22]})', callback_data=f'mn:remdel:{r["id"]}')])
    await say(update, '\n'.join(lines), keyboard=InlineKeyboardMarkup(buttons) if buttons else None)


async def reminder_delete(update, context):
    if not await guard(update): return
    m = re.match(r'^/apagar_lembrete(?:@\w+)?\s+(\d+)\s*$', update.message.text.strip())
    if not m:
        await say(update, 'Formato: /apagar_lembrete <id>\nVeja os ids em /lembretes.'); return
    rid = int(m.group(1))
    rem = db.get_reminder(rid)
    if not rem:
        await say(update, f'Lembrete pendente #{rid} não encontrado.'); return
    due_str = datetime.fromisoformat(rem['due_at']).astimezone(TZ).strftime('%d/%m/%Y %H:%M')
    action = {'type': 'reminder_delete', 'rid': rid, 'content': rem['content']}
    await ask_action_confirmation(update, context, action,
        f'Cancelar este lembrete?\n\n#{rid} {due_str} — {rem["content"]}')


def mode_keyboard():
    current = db.get_mode()
    auto_icon = '🔘 ' if current == 'auto' else ''
    rapido_icon = '🔘 ' if current == 'rapido' else ''
    avancado_icon = '🔘 ' if current == 'avancado' else ''
    gemini_icon = '🔘 ' if current == 'gemini' else ''
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f'{auto_icon}⚡ Automático (Recomendado)', callback_data='mode:auto')],
        [InlineKeyboardButton(f'{rapido_icon}🚀 Rápido (Lightning - 30B)', callback_data='mode:rapido')],
        [InlineKeyboardButton(f'{avancado_icon}🧠 Avançado (Ultra - 550B)', callback_data='mode:avancado')],
        [InlineKeyboardButton(f'{gemini_icon}✨ Gemini (Google)', callback_data='mode:gemini')],
    ])


async def mode_set(update, context):
    if not await guard(update): return
    if not getattr(context, 'args', None):
        current = db.get_mode()
        await _msg(update).reply_text(
            f'⚙️ *Configuração do Modelo de IA*\n\n'
            f'• *Modelo atual:* `{current}`\n\n'
            f'Escolha o modo de operação desejado:',
            reply_markup=mode_keyboard(),
            parse_mode='Markdown'
        )
        return
    try:
        mode = context.args[0].lower()
        if mode == 'gemini' and not MODELS['gemini'][1]:
            await say(update, 'Gemini ainda não está configurado: coloque GEMINI_API_KEY no .env e reinicie o bot.')
            return
        db.set_mode(mode)
        await say(update, f'✅ Modelo configurado para: {mode}.')
    except ValueError as exc:
        await say(update, str(exc))


async def mode_callback(update, context):
    query = update.callback_query
    if not await guard(update): return
    await safe_answer(query)
    try:
        _, mode = query.data.split(':', 1)
        if mode == 'gemini' and not MODELS['gemini'][1]:
            await query.edit_message_text('Gemini ainda não está configurado: coloque GEMINI_API_KEY no .env e reinicie o bot.',
                                          reply_markup=mode_keyboard())
            return
        db.set_mode(mode)
        await query.edit_message_text(
            f'✅ *Modelo alterado com sucesso!*\n\n'
            f'• *Novo modo ativo:* `{mode}`',
            reply_markup=mode_keyboard(),
            parse_mode='Markdown'
        )
    except Exception:
        await query.edit_message_text('Não foi possível alterar o modelo. Tente novamente.')


def make_backup(directory):
    """Cópia consistente do banco (API de backup do SQLite funciona mesmo com WAL)."""
    outfile = Path(directory) / ('financeai-backup-' + db.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
    original = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(outfile)
    try:
        original.backup(dst)
    finally:
        original.close()
        dst.close()
    return outfile


async def backup(update, context):
    if not await guard(update):return
    with tempfile.TemporaryDirectory() as tmp:
        outfile = make_backup(tmp)
        with outfile.open('rb') as f:
            await _msg(update).reply_document(f,filename=outfile.name,caption='Backup completo: mantenha este arquivo privado. Faça cópias fora do celular.')


async def reset_db_cmd(update, context):
    if not await guard(update): return
    action = {'type': 'reset_all'}
    await ask_action_confirmation(update, context, action,
        '⚠️ ATENÇÃO: Deseja realmente ZERAR todo o banco de dados (transações, contas a pagar, '
        'contas fixas, lembretes, perfil e histórico)?\n\n'
        'Um backup completo será enviado automaticamente neste chat antes de limpar tudo.')


def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('💳 Pagar conta em aberto', callback_data='mn:paylist:0'),
         InlineKeyboardButton('✏️ Editar conta (data/valor)', callback_data='mn:editdebt_list:0')],
        [InlineKeyboardButton('🗑️ Excluir conta', callback_data='mn:deldebt_list:0'),
         InlineKeyboardButton('🔁 Contas fixas', callback_data='mn:rec_list')],
        [InlineKeyboardButton('⏰ Lembretes', callback_data='mn:rem_list'),
         InlineKeyboardButton('📜 Apagar/Estornar lançamento', callback_data='mn:txdel_list')],
        [InlineKeyboardButton('📊 Relatório + CSV', callback_data='mn:report'),
         InlineKeyboardButton('⚙️ Modelo de IA', callback_data='mn:mode')],
        [InlineKeyboardButton('🗄️ Baixar Backup', callback_data='mn:backup'),
         InlineKeyboardButton('🕒 Relógio do Bot', callback_data='mn:clock')],
        [InlineKeyboardButton('🔋 Bateria do Celular', callback_data='mn:battery'),
         InlineKeyboardButton('❓ Ajuda', callback_data='mn:help')],
    ])


async def menu_cmd(update, context):
    if not await guard(update): return
    await say(
        update,
        '🛠️ *Menu e Opções do FinanceAI*\n\n'
        'Toque na função desejada abaixo sem precisar digitar comandos:',
        keyboard=menu_keyboard()
    )


async def menu_callback(update, context):
    query = update.callback_query
    if not await guard(update): return
    await safe_answer(query)
    parts = (query.data or '').split(':')
    if len(parts) < 2:
        return
    cmd = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    if cmd == 'debts':
        await debt_list(update, context, month=arg)
    elif cmd == 'rec_list':
        await recurring_list(update, context)
    elif cmd == 'rem_list':
        await reminder_list(update, context)
    elif cmd == 'report' or cmd == 'rep':
        await report(update, context, month=arg)
    elif cmd == 'repcsv':
        month = arg or db.now().strftime('%Y-%m')
        doc = build_csv(month)
        if doc:
            await _msg(update).reply_document(doc, filename=doc.name, caption=f'📄 Extrato detalhado ({month[5:]}/{month[:4]})')
        else:
            await say(update, f'Não há movimentações registradas em {month[5:]}/{month[:4]} para gerar planilha.')
    elif cmd == 'plan':
        await plan(update, context, month=arg)
    elif cmd == 'qinc':
        presets = {
            'sal15': (93500, 'Salário', 'Salário (1ª parcela dia 15)'),
            'sal30': (83000, 'Salário', 'Salário (parcela fim do mês)'),
            'joaninha': (7000, 'Renda Fixa', 'Sistema Joaninha baby kids'),
        }
        amount, cat, desc = presets.get(arg, (0, 'Receita', 'Receita'))
        if amount <= 0:
            return
        action = {'type': 'transaction', 'kind': 'receita', 'amount': amount,
                  'category': cat, 'description': desc, 'occurred_date': None}
        await ask_action_confirmation(
            update, context, action,
            f'Confira antes de registrar:\n\nTipo: Receita\nValor: {db.brl(amount)}\nCategoria: {cat}\nDescrição: {desc}\n\nPosso salvar no seu histórico financeiro?'
        )
    elif cmd == 'qexp':
        presets = {
            'comb20': (2000, 'Transporte', 'Combustível'),
        }
        amount, cat, desc = presets.get(arg, (0, 'Outros', 'Despesa'))
        if amount <= 0:
            return
        action = {'type': 'transaction', 'kind': 'despesa', 'amount': amount,
                  'category': cat, 'description': desc, 'occurred_date': None}
        await ask_action_confirmation(
            update, context, action,
            f'Confira antes de registrar:\n\nTipo: Despesa\nValor: {db.brl(amount)}\nCategoria: {cat}\nDescrição: {desc}\n\nPosso salvar no seu histórico financeiro?'
        )
    elif cmd == 'mode':
        await mode_set(update, context)
    elif cmd == 'backup':
        await backup(update, context)
    elif cmd == 'clock':
        await clock_cmd(update, context)
    elif cmd == 'battery':
        await battery_cmd(update, context)
    elif cmd == 'reset':
        await reset_db_cmd(update, context)
    elif cmd == 'help':
        await help_cmd(update, context)
    elif cmd == 'hist':
        kind = None if arg in (None, 'all') else arg
        await history(update, context, kind)
    elif cmd == 'paylist':
        page = int(arg or 0)
        debts = db.get_debts()
        if not debts:
            await say(update, '🎉 Nenhuma conta em aberto para pagar.')
            return
        per_page = 8
        chunk = debts[page * per_page:(page + 1) * per_page]
        buttons = []
        for d in chunk:
            rem = d['original_amount'] - d['paid_amount']
            due_br = db.short_date(d['due_date'])[:5]
            buttons.append([InlineKeyboardButton(
                f'💳 #{d["id"]} {d["title"][:22]} — {db.brl(rem)} ({due_br})',
                callback_data=f'mn:paypick:{d["id"]}'
            )])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton('⬅️ Anteriores', callback_data=f'mn:paylist:{page - 1}'))
        if (page + 1) * per_page < len(debts):
            nav.append(InlineKeyboardButton('Próximas ➡️', callback_data=f'mn:paylist:{page + 1}'))
        if nav:
            buttons.append(nav)
        await say(update, '💳 *Selecione a conta que você pagou:*', keyboard=InlineKeyboardMarkup(buttons))
    elif cmd == 'paypick':
        debt_id = int(arg)
        d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
        if not d:
            await say(update, 'Conta não encontrada ou já quitada.')
            return
        rem = d['original_amount'] - d['paid_amount']
        buttons = [[InlineKeyboardButton(f'✅ Pagar total ({db.brl(rem)})', callback_data=f'mn:payamt:{debt_id}:{rem}')]]
        if rem > 10000:
            buttons.append([InlineKeyboardButton('💵 Pagar R$ 100,00', callback_data=f'mn:payamt:{debt_id}:10000')])
        await say(
            update,
            f'📌 *Conta #{d["id"]}: {d["title"]}*\n'
            f'• Restante: `{db.brl(rem)}`\n'
            f'• Vencimento: `{db.short_date(d["due_date"])}`\n\n'
            'Toque abaixo para registrar o pagamento (ou escreva no chat o valor exato se for outro valor parcial):',
            keyboard=InlineKeyboardMarkup(buttons)
        )
    elif cmd == 'payamt':
        debt_id = int(arg)
        amount = int(parts[3])
        d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
        if not d:
            await say(update, 'Conta não encontrada ou já quitada.')
            return
        rem = d['original_amount'] - d['paid_amount']
        amount = min(amount, rem)
        action = {'type': 'debt_payment', 'debt_id': debt_id, 'amount': amount, 'title': d['title']}
        await ask_action_confirmation(
            update, context, action,
            f'Confirmar registro de pagamento?\n\n'
            f'Conta: {d["title"]}\n'
            f'Valor pago: {db.brl(amount)}\n'
            f'Restante após registro: {db.brl(rem - amount)}'
        )
    elif cmd == 'deldebt_list':
        page = int(arg or 0)
        debts = db.get_debts()
        if not debts:
            await say(update, 'Não há contas em aberto para excluir.')
            return
        per_page = 8
        chunk = debts[page * per_page:(page + 1) * per_page]
        buttons = [
            [InlineKeyboardButton(f'🗑️ #{d["id"]} {d["title"][:24]} ({db.brl(d["original_amount"] - d["paid_amount"])})',
                                  callback_data=f'mn:deldebt:{d["id"]}')]
            for d in chunk
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton('⬅️ Anteriores', callback_data=f'mn:deldebt_list:{page - 1}'))
        if (page + 1) * per_page < len(debts):
            nav.append(InlineKeyboardButton('Próximas ➡️', callback_data=f'mn:deldebt_list:{page + 1}'))
        if nav:
            buttons.append(nav)
        await say(update, '🗑️ *Selecione a conta que deseja excluir:*', keyboard=InlineKeyboardMarkup(buttons))
    elif cmd == 'editdebt_list':
        page = int(arg or 0)
        debts = db.get_debts()
        if not debts:
            await say(update, 'Não há contas em aberto para editar.')
            return
        per_page = 8
        chunk = debts[page * per_page:(page + 1) * per_page]
        buttons = [
            [InlineKeyboardButton(
                f'✏️ #{d["id"]} {d["title"][:22]} ({db.short_date(d["due_date"])[:5]})',
                callback_data=f'mn:editpick:{d["id"]}'
            )]
            for d in chunk
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton('⬅️ Anteriores', callback_data=f'mn:editdebt_list:{page - 1}'))
        if (page + 1) * per_page < len(debts):
            nav.append(InlineKeyboardButton('Próximas ➡️', callback_data=f'mn:editdebt_list:{page + 1}'))
        if nav:
            buttons.append(nav)
        await say(update, '✏️ *Selecione a conta para alterar a data ou valor:*', keyboard=InlineKeyboardMarkup(buttons))
    elif cmd == 'editpick':
        debt_id = int(arg)
        d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
        if not d:
            await say(update, 'Conta não encontrada.')
            return
        rem = d['original_amount'] - d['paid_amount']
        cur_due = date.fromisoformat(d['due_date'])
        d_plus1 = (cur_due + timedelta(days=1)).isoformat()
        d_plus5 = (cur_due + timedelta(days=5)).isoformat()
        d_plus7 = (cur_due + timedelta(days=7)).isoformat()
        context.application.bot_data.setdefault('edit_debt_state', {})[update.effective_user.id] = {
            'debt_id': debt_id,
            'updated_at': db.now().timestamp(),
        }
        buttons = [
            [InlineKeyboardButton(f'📅 +1 dia ({db.short_date(d_plus1)[:5]})', callback_data=f'mn:editdate:{debt_id}:{d_plus1}'),
             InlineKeyboardButton(f'📅 +5 dias ({db.short_date(d_plus5)[:5]})', callback_data=f'mn:editdate:{debt_id}:{d_plus5}')],
            [InlineKeyboardButton(f'📅 +7 dias ({db.short_date(d_plus7)[:5]})', callback_data=f'mn:editdate:{debt_id}:{d_plus7}')],
        ]
        await say(
            update,
            f'✏️ *Editando Conta #{d["id"]}: {d["title"]}*\n'
            f'• Vencimento atual: `{db.short_date(d["due_date"])}`\n'
            f'• Valor restante: `{db.brl(rem)}`\n\n'
            'Toque em um botão rápido de data abaixo **ou digite no chat**:\n'
            '• Só o novo valor (ex.: `20,00`)\n'
            '• Só a nova data (ex.: `15/11/2026` ou `15/11`)\n'
            '• Ou valor e data juntos (ex.: `20,00 15/11/2026`)',
            keyboard=InlineKeyboardMarkup(buttons)
        )
    elif cmd == 'recedit':
        rec_id = int(arg)
        rec = next((r for r in db.get_recurring(active_only=False) if r['id'] == rec_id), None)
        if not rec:
            await say(update, 'Conta fixa não encontrada.')
            return
        context.application.bot_data.setdefault('edit_debt_state', {})[update.effective_user.id] = {
            'rec_id': rec_id,
            'updated_at': db.now().timestamp(),
        }
        await say(
            update,
            f'✏️ *Editando Conta Fixa #{rec["id"]}: {rec["title"]}*\n'
            f'• Valor atual: `{db.brl(rec["amount"])}`\n\n'
            'Digite no chat o **novo valor** dessa conta fixa (ex.: `20,00`).\n'
            '*(Isso também atualizará automaticamente as contas em aberto geradas por ela!)*'
        )
    elif cmd == 'editdate':
        debt_id = int(arg)
        new_due = parts[3]
        d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
        if not d:
            await say(update, 'Conta não encontrada.')
            return
        context.application.bot_data.setdefault('edit_debt_state', {}).pop(update.effective_user.id, None)
        action = {'type': 'debt_edit', 'debt_id': debt_id, 'title': d['title'], 'new_due_date': new_due, 'new_total': d['original_amount']}
        await ask_action_confirmation(
            update, context, action,
            f'Alterar vencimento da conta #{debt_id} ({d["title"]})?\n\n'
            f'De: {db.short_date(d["due_date"])}\n'
            f'Para: {db.short_date(new_due)}'
        )
    elif cmd == 'deldebt':
        debt_id = int(arg)
        d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
        if not d:
            await say(update, 'Conta não encontrada.')
            return
        rem = d['original_amount'] - d['paid_amount']
        action = {'type': 'debt_delete', 'debt_id': debt_id, 'title': d['title']}
        await ask_action_confirmation(
            update, context, action,
            f'Excluir a conta #{debt_id} ({d["title"]} — {db.brl(rem)})?'
        )
    elif cmd == 'recdel':
        rec_id = int(arg)
        rec = next((r for r in db.get_recurring(active_only=False) if r['id'] == rec_id), None)
        if not rec:
            await say(update, 'Conta fixa não encontrada.')
            return
        action = {'type': 'recurring_delete', 'rec_id': rec_id, 'title': rec['title']}
        await ask_action_confirmation(
            update, context, action,
            f'Excluir a conta fixa #{rec_id} ({rec["title"]} — {db.brl(rec["amount"])})?'
        )
    elif cmd == 'remdel':
        rid = int(arg)
        rem = db.get_reminder(rid)
        if not rem:
            await say(update, 'Lembrete pendente não encontrado.')
            return
        due_str = datetime.fromisoformat(rem['due_at']).astimezone(TZ).strftime('%d/%m/%Y %H:%M')
        action = {'type': 'reminder_delete', 'rid': rid, 'content': rem['content']}
        await ask_action_confirmation(
            update, context, action,
            f'Cancelar este lembrete?\n\n#{rid} {due_str} — {rem["content"]}'
        )
    elif cmd == 'txdel_list':
        txs = db.get_transactions(10)
        if not txs:
            await say(update, 'Não há lançamentos recentes para apagar ou estornar.')
            return
        buttons = []
        for t in txs:
            label_act = '↩️ Estornar' if t['debt_id'] else '🗑️ Apagar'
            buttons.append([InlineKeyboardButton(
                f'{label_act} #{t["id"]} {db.brl(t["amount"])} ({t["description"][:18]})',
                callback_data=f'mn:txdel:{t["id"]}'
            )])
        await say(update, '📜 *Selecione o lançamento que deseja apagar ou estornar:*', keyboard=InlineKeyboardMarkup(buttons))
    elif cmd == 'txdel':
        tx_id = int(arg)
        tx = db.get_transaction(tx_id)
        if not tx:
            await say(update, 'Lançamento não encontrado.')
            return
        if tx['debt_id']:
            action = {'type': 'tx_refund_debt', 'tx_id': tx['id']}
            await ask_action_confirmation(
                update, context, action,
                f'Estornar este pagamento de dívida?\n\n#{tx["id"]} Pagamento de {db.brl(tx["amount"])}\n'
                f'Conta: {tx["description"]} (Dívida #{tx["debt_id"]})\n\n'
                'O valor voltará a ficar em aberto na conta e o lançamento será removido.'
            )
        else:
            action = {'type': 'tx_delete', 'tx_id': tx['id']}
            await ask_action_confirmation(
                update, context, action,
                f'Apagar este lançamento?\n\n#{tx["id"]} {tx["kind"].capitalize()} {db.brl(tx["amount"])}\n'
                f'{tx["category"]} — {tx["description"]}'
            )
    elif cmd == 'import_init':
        action = {'type': 'import_initial'}
        await ask_action_confirmation(
            update, context, action,
            '📥 Deseja carregar todos os seus dados iniciais configurados (receitas e pagamentos de 30/10/2026, '
            'contas fixas mensais/semanais, faturas Shopee e cartões)?\n\n'
            '⚠️ Isso limpará os dados antigos e deixará seu saldo inicial exatamente em R$ 2.446,19.'
        )


TYPING_INTERVAL = 4
SLOW_NOTICE_AFTER = 25


class Waiting:
    """Mantém o 'digitando...' ativo e avisa uma vez quando a IA está demorando."""

    def __init__(self, update):
        self.update = update
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        started = time.monotonic()
        warned = False
        while True:
            try:
                await self.update.message.chat.send_action('typing')
                if not warned and time.monotonic() - started >= SLOW_NOTICE_AFTER:
                    warned = True
                    await self.update.message.reply_text('⏳ A IA gratuita está demorando. Continuo aguardando a resposta...')
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(TYPING_INTERVAL)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


INSTANT_TEXTS = {normalized(t) for t in (
    '💰 Saldo', '➕ Registrar receita', '➖ Registrar despesa',
    '📅 Contas a pagar', '📊 Relatório', '📜 Histórico',
    '🔁 Contas fixas', '⏰ Lembretes', '🧭 Planejamento',
    '🛠️ Menu e Opções', '⚙️ Modelo de IA',
)}


async def chat(update, context):
    """Mensagens do mesmo usuário são tratadas em ordem: a resposta rápida a uma pergunta da IA
    (ex.: "50 reais") só é lida depois que o rascunho anterior existir. Atalhos não esperam."""
    prompt = ((update.message.text if update.message else '') or '').strip()
    if update.effective_user is None or not allowed(update) or normalized(prompt) in INSTANT_TEXTS or asks_battery(prompt):
        await _chat(update, context)
        return
    lock = context.application.bot_data.setdefault('chat_locks', {}).setdefault(update.effective_user.id, asyncio.Lock())
    async with lock:
        await _chat(update, context)


async def _chat(update, context):
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
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🟢 Salário Dia 15 (R$ 935,00)', callback_data='mn:qinc:sal15')],
            [InlineKeyboardButton('🟢 Salário Fim do Mês (R$ 830,00)', callback_data='mn:qinc:sal30')],
            [InlineKeyboardButton('🟢 Sistema Joaninha (R$ 70,00)', callback_data='mn:qinc:joaninha')],
        ])
        await say(
            update,
            '🟢 *Registrar Receita*\n\n'
            'Toque em uma das suas receitas fixas abaixo para registrar com 1 clique, '
            'ou escreva normalmente no chat (ex.: “Recebi R$ 150 de serviço”):',
            keyboard=kb
        )
        return
    if shortcut == normalized('➖ Registrar despesa'):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('💳 Pagar uma conta cadastrada', callback_data='mn:paylist:0')],
            [InlineKeyboardButton('⛽ Combustível avulso (R$ 20,00)', callback_data='mn:qexp:comb20')],
        ])
        await say(
            update,
            '🔴 *Registrar Despesa ou Pagamento*\n\n'
            'Se for o pagamento de uma conta já cadastrada, toque em **💳 Pagar uma conta cadastrada** abaixo.\n'
            'Se for um gasto avulso do dia a dia, escreva normalmente no chat (ex.: “Gastei R$ 45,90 no almoço”):',
            keyboard=kb
        )
        return
    if shortcut == normalized('📅 Contas a pagar'):
        await debt_list(update, context); return
    if shortcut == normalized('📊 Relatório'):
        await report(update, context); return
    if shortcut == normalized('📜 Histórico'):
        await history(update, context); return
    if shortcut == normalized('🔁 Contas fixas'):
        await recurring_list(update, context); return
    if shortcut == normalized('⏰ Lembretes'):
        await reminder_list(update, context); return
    if shortcut == normalized('🧭 Planejamento'):
        await plan(update, context); return
    if shortcut == normalized('🛠️ Menu e Opções'):
        await menu_cmd(update, context); return
    if shortcut == normalized('⚙️ Modelo de IA'):
        await mode_set(update, context); return

    if asks_battery(prompt):
        await battery_cmd(update, context); return

    wanted = asks_history(prompt)
    if wanted:
        await history(update, context, None if wanted == 'todas' else wanted); return

    user_id = update.effective_user.id
    edit_states = context.application.bot_data.setdefault('edit_debt_state', {})
    edit_state = edit_states.get(user_id)
    if edit_state and db.now().timestamp() - edit_state.get('updated_at', 0) > 180:
        edit_states.pop(user_id, None)
        edit_state = None
    if edit_state:
        if shortcut in {'cancelar', 'cancela', 'sair', 'parar', 'nao'}:
            edit_states.pop(user_id, None)
            await say(update, 'Edição cancelada.')
            return
        if 'rec_id' in edit_state:
            try:
                tokens = [t for t in prompt.split() if re.search(r'\d', t)]
                new_amt = db.money_to_cents(tokens[-1] if tokens else prompt)
                rec_id = edit_state['rec_id']
                rec = next((r for r in db.get_recurring(active_only=False) if r['id'] == rec_id), None)
                edit_states.pop(user_id, None)
                if not rec:
                    await say(update, 'Conta fixa não encontrada.')
                    return
                action = {'type': 'recurring_edit', 'rec_id': rec_id, 'title': rec['title'], 'new_amount': new_amt}
                await ask_action_confirmation(
                    update, context, action,
                    f'Alterar valor da conta fixa #{rec_id} ({rec["title"]}) e de suas faturas em aberto?\n\n'
                    f'De: {db.brl(rec["amount"])}\n'
                    f'Para: {db.brl(new_amt)}'
                )
                return
            except ValueError as exc:
                await say(update, f'Valor inválido: {exc}. Digite apenas o valor, por exemplo: 20,00')
                return
        rest = prompt
        due_iso = None
        m_dt = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b', rest)
        if m_dt:
            day_s, mon_s, yr_s = m_dt.group(1), m_dt.group(2), m_dt.group(3) or str(db.now().year)
            try:
                due_iso = db.parse_date(f'{int(day_s):02d}/{int(mon_s):02d}/{yr_s}')
            except ValueError as exc:
                await say(update, f'Data inválida: {exc}. Digite no formato DD/MM/AAAA (ex.: 15/11/2026).')
                return
            rest = (rest[:m_dt.start()] + ' ' + rest[m_dt.end():]).strip()
        new_amt = None
        tokens = [t for t in rest.split() if re.search(r'\d', t)]
        if tokens:
            try:
                new_amt = db.money_to_cents(tokens[-1])
            except ValueError:
                new_amt = None
        if due_iso is not None or new_amt is not None:
            debt_id = edit_state['debt_id']
            d = next((row for row in db.get_debts() if row['id'] == debt_id), None)
            edit_states.pop(user_id, None)
            if not d:
                await say(update, 'Conta não encontrada.')
                return
            new_total = (d['paid_amount'] + new_amt) if new_amt is not None else d['original_amount']
            final_due = due_iso or d['due_date']
            action = {'type': 'debt_edit', 'debt_id': debt_id, 'title': d['title'],
                      'new_due_date': final_due, 'new_total': new_total}
            await ask_action_confirmation(
                update, context, action,
                f'Confirmar alteração da conta #{debt_id} ({d["title"]})?\n\n'
                f'• Valor restante: {db.brl(new_total - d["paid_amount"])}\n'
                f'• Vencimento: {db.short_date(final_due)}'
            )
            return

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

    waiter = Waiting(update)
    waiter.start()
    try:
        # Perguntas gerais vão direto ao chat. Mensagens sobre dinheiro passam pelo interpretador.
        if not previous and not looks_financial(prompt):
            result_text = await ai.answer(prompt)
            await say(update, result_text)
            return

        try:
            result = await ai.interpret_message(prompt, previous)
        except RuntimeError:
            if previous or not quick.is_record_attempt(prompt):
                raise
            result = {'type': 'chat'}
        intent = result.get('type', 'chat')
        if not previous and intent not in ('transaction', 'debt', 'debt_payment', 'reminder', 'clarify'):
            fallback = quick.parse(prompt, db.now().date())
            if fallback:
                result, intent = fallback, 'transaction'
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
        if quick.is_record_attempt(prompt):
            await say(update, '⚠️ Não consegui entender esse lançamento, então NÃO registrei nada. '
                              'Reenvie com valor e descrição (ex.: “Recebi R$ 831 de complemento de salário”) '
                              'ou use /receita e /despesa.')
            return
        result_text = await ai.answer(prompt)
        await say(update, result_text)
    except RuntimeError as exc:
        log.warning('Falha na IA: %s', exc)
        await say(update, 'Não consegui processar sua mensagem pela IA: ' + str(exc))
    except Exception:
        log.exception('Erro inesperado sem registrar a mensagem')
        await say(update, 'Ocorreu um erro inesperado. Nenhum lançamento foi feito. Tente novamente ou verifique o log do Termux.')
    finally:
        await waiter.stop()


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
        # Não repete a pergunta em loop: oferece quitar só o que falta e deixa a diferença explícita.
        context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
        action = {'type': 'debt_payment', 'debt_id': debt['id'], 'amount': remaining, 'title': debt['title']}
        await ask_action_confirmation(update, context, action,
            f'Você informou {db.brl(amount)}, mas a conta “{debt["title"]}” só tem {db.brl(remaining)} em aberto.\n\n'
            f'Posso registrar a quitação de {db.brl(remaining)}? A diferença de {db.brl(amount - remaining)} '
            f'NÃO será lançada; se foi um gasto extra, registre-o à parte (ex.: “gastei {db.brl(amount - remaining)[3:]} com gorjeta”).')
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
    now = db.now()
    # A data/hora escrita pelo usuário vale mais que a conta feita pela IA.
    text = ' '.join(filter(None, [(previous or {}).get('original_message'), prompt]))
    parsed = when.resolve(text, now)
    if parsed is None and reminder_at:
        try:
            # A IA devolve hora local; qualquer fuso/offset que ela escreva é ignorado.
            parsed = datetime.fromisoformat(str(reminder_at)).replace(tzinfo=TZ)
        except (ValueError, TypeError):
            await store_clarification(update, context, prompt, result,
                'Não consegui entender a data e hora. Informe uma data e horário futuros, por favor.', previous)
            return
    if parsed is None:
        await store_clarification(update, context, prompt, result,
            'Em qual dia e horário devo lembrar você? Informe a data e a hora.', previous)
        return
    if parsed <= now:
        await store_clarification(update, context, prompt, result,
            'Essa data e hora já passaram. Qual data e horário futuros você prefere?', previous)
        return

    due_at = parsed.isoformat()
    action = {'type': 'reminder', 'due_at': due_at, 'content': content}
    context.application.bot_data.setdefault('natural_drafts', {}).pop(user_id, None)
    await ask_action_confirmation(update, context, action,
        f'Confira o lembrete:\n\nLembrar: {content}\n'
        f'Quando: {ai.WEEKDAYS[parsed.weekday()]}, {parsed.strftime("%d/%m/%Y às %H:%M")} ({when.humanize(parsed - db.now())})\n\n'
        'Posso salvar esse lembrete?')


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
        log.info('Guardrail: query help bloqueado para mensagem financeira')
        result_text = await ai.answer(prompt)
        await say(update, result_text)
        return True

    # Guardrail: nunca disparar debt_list (debts) se a mensagem é sobre recebimento ou salário.
    if str(query or '').strip().lower() == 'debts' and (
        re.search(r'\b(receb|sal[aá]rio|complemento|adiantamento|ganh|renda|prefeitura|contrato|a receber)\b', prompt, re.I)
    ):
        log.info('Guardrail: query debts bloqueado para mensagem de recebimento')
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
    await safe_answer(query)
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
        elif action_type == 'tx_refund_debt':
            info = db.refund_debt_payment(int(action['tx_id']))
            await query.edit_message_text(
                f'↩️ Pagamento #{action["tx_id"]} estornado ({db.brl(info["amount"])}).\n'
                f'Conta “{info["title"]}”: restante voltou para {db.brl(info["remaining"])}.\n'
                f'Saldo histórico registrado: {db.brl(db.summary()["total"])}')
        elif action_type == 'debt_edit':
            remaining = db.update_debt(int(action['debt_id']), int(action['new_total']), new_due_date=action.get('new_due_date'))
            await query.edit_message_text(
                f'✅ Conta #{action["debt_id"]} ({action.get("title", "")}) atualizada!\n'
                f'• Novo vencimento: {db.short_date(action["new_due_date"])}\n'
                f'• Restante: {db.brl(remaining)}')
        elif action_type == 'recurring_edit':
            info = db.update_recurring(int(action['rec_id']), int(action['new_amount']), update_open_debts=True)
            await query.edit_message_text(
                f'✅ Conta fixa #{action["rec_id"]} ({info["title"]}) e suas faturas em aberto foram atualizadas para {db.brl(info["amount"])}!')
        elif action_type == 'debt_delete':
            db.remove_debt(int(action['debt_id']))
            await query.edit_message_text(
                f'🗑️ Conta #{action["debt_id"]} ({action.get("title", "")}) excluída com sucesso.')
        elif action_type == 'recurring_delete':
            db.remove_recurring(int(action['rec_id']))
            await query.edit_message_text(
                f'🗑️ Conta fixa #{action["rec_id"]} ({action.get("title", "")}) excluída.')
        elif action_type == 'reminder_delete':
            db.delete_reminder(int(action['rid']))
            await query.edit_message_text(
                f'🗑️ Lembrete #{action["rid"]} cancelado.')
        elif action_type == 'import_initial':
            target = BASE / 'importar.json' if (BASE / 'importar.json').exists() else BASE / 'dados_iniciais.json'
            importar.import_financial_data(str(target))
            s = db.summary()
            debts = db.get_debts()
            recs = db.get_recurring()
            await query.edit_message_text(
                f'✅ Dados iniciais importados com sucesso!\n\n'
                f'💰 Saldo atual em caixa: {db.brl(s["total"])}\n'
                f'🟢 Receitas registradas: {db.brl(s["receita"])}\n'
                f'🔴 Despesas pagas hoje: {db.brl(s["despesa"])}\n'
                f'🔁 Contas fixas configuradas: {len(recs)}\n'
                f'📅 Contas e parcelas em aberto: {len(debts)}')
        elif action_type == 'reset_all':
            if getattr(query, 'message', None) and hasattr(query.message, 'reply_document'):
                with tempfile.TemporaryDirectory() as tmp:
                    outfile = make_backup(tmp)
                    with outfile.open('rb') as f:
                        await query.message.reply_document(
                            f, filename=outfile.name,
                            caption='🗄 Backup de segurança gerado antes de zerar o banco.')
            db.reset_all()
            await query.edit_message_text(
                '🧹 Banco de dados zerado com sucesso! Todos os registros foram limpos para você começar do zero.')
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


def month_report_text(month):
    s = db.summary(month)
    year, month_num = month.split('-')
    lines = [
        f'📊 *RELATÓRIO FINANCEIRO — {month_num}/{year}*\n',
        f'🟢 *Total de Receitas:* `{db.brl(s["receita"])}`',
        f'🔴 *Total de Despesas:* `{db.brl(s["despesa"])}`',
        f'💰 *Saldo em Caixa:* `{db.brl(s["total"])}`\n',
        '📂 *Maiores Gastos por Categoria:*'
    ]
    if s['categories']:
        lines += [f'  • {c["category"]}: `{db.brl(c["total"])}`' for c in s['categories']]
    else:
        lines.append('  • Nenhum gasto registrado neste mês.')
    return '\n'.join(lines)


def build_csv(month):
    """CSV (BytesIO nomeado) das movimentações do mês, ou None se não houver."""
    rows = db.month_transactions(month)
    if not rows:
        return None
    year, month_num = month.split('-')
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['ID', 'Tipo', 'Valor (R$)', 'Categoria', 'Descrição', 'Data'])
    for r in rows:
        val_fmt = f'{r["amount"] / 100:.2f}'.replace('.', ',')
        writer.writerow([r['id'], r['kind'].capitalize(), val_fmt, r['category'],
                         r['description'], db.short_date(r['occurred_at'][:10])])
    doc = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    doc.name = f'relatorio-{month_num}-{year}.csv'
    return doc


def format_morning(data):
    """Texto HTML do resumo matinal."""
    s = data['summary']
    lines = ['☀️ <b>Bom dia! Resumo do seu dia</b>\n',
             f'💰 Saldo registrado: <code>{html.escape(db.brl(s["total"]))}</code>',
             f'🟢 Receitas do mês: <code>{html.escape(db.brl(s["receita"]))}</code>',
             f'🔴 Despesas do mês: <code>{html.escape(db.brl(s["despesa"]))}</code>\n']
    if data.get('battery'):
        lines.insert(4, f'🔋 Bateria do celular: {data["battery"]["pct"]}%\n')
    if data['debts']:
        lines.append('📅 <b>Contas (atrasadas e próximos 7 dias):</b>')
        for d in data['debts']:
            n = d['days_left']
            when = (f'atrasada há {-n} dia{"s" if n < -1 else ""}' if n < 0
                    else 'vence HOJE' if n == 0 else 'vence amanhã' if n == 1 else f'vence em {n} dias')
            lines.append(f'• {html.escape(str(d["title"]))} — {html.escape(db.brl(d["remaining"]))} ({when}, {db.short_date(d["due_date"])})')
    else:
        lines.append('📅 Nenhuma conta vencendo nos próximos 7 dias. 🎉')
    if data['reminders']:
        lines.append('\n⏰ <b>Lembretes de hoje:</b>')
        for r in data['reminders']:
            hour = datetime.fromisoformat(r['due_at']).astimezone(TZ).strftime('%H:%M')
            lines.append(f'• {hour} — {html.escape(r["content"])}')
    return '\n'.join(lines)


async def daily_jobs(application):
    """Resumo matinal (1x/dia) e relatório do mês anterior (1x/mês, nos primeiros dias)."""
    moment = db.now()
    if moment.hour < MORNING_HOUR:
        return
    today = moment.date().isoformat()
    if db.get_pref('last_morning_summary') != today:
        data = db.morning_data()
        data['battery'] = await battery.read_status() if BATTERY_ALERTS else None
        await application.bot.send_message(OWNER_ID, format_morning(data), parse_mode='HTML')
        db.set_pref('last_morning_summary', today)
    prev = db.previous_month()
    if moment.day <= 5 and db.get_pref('last_monthly_report') != prev:
        text = month_report_text(prev)
        try:
            await application.bot.send_message(OWNER_ID, text, parse_mode='Markdown')
        except Exception:
            await application.bot.send_message(OWNER_ID, text)
        doc = build_csv(prev)
        if doc:
            await application.bot.send_document(OWNER_ID, doc, filename=doc.name,
                                                caption=f'📄 Extrato automático de {prev[5:]}/{prev[:4]}')
        db.set_pref('last_monthly_report', prev)
    last = db.get_pref('last_auto_backup')
    if last is None or (moment.date() - date.fromisoformat(last)).days >= 7:
        with tempfile.TemporaryDirectory() as tmp:
            outfile = make_backup(tmp)
            with outfile.open('rb') as f:
                await application.bot.send_document(
                    OWNER_ID, f, filename=outfile.name,
                    caption='🗄 Backup semanal automático do banco. Guarde fora do celular (Drive, e-mail).')
        db.set_pref('last_auto_backup', today)


async def alert_callback(update, context):
    """Botões das mensagens automáticas: adiar lembrete, registrar pagamento, lembrar de conta."""
    q = update.callback_query
    if not await guard(update): return
    await safe_answer(q)
    try:
        prefix, action, num = q.data.split(':')
        num = int(num)
    except ValueError:
        return
    original = q.message.text or ''
    now = db.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    if prefix == 'rm':
        due = now + timedelta(hours=1) if action == 'h1' else tomorrow
        if db.snooze_reminder(num, due.isoformat()):
            await q.edit_message_text(f'{original}\n\n⏰ Adiado para {due.strftime("%d/%m às %H:%M")}.')
        else:
            await q.edit_message_text(f'{original}\n\nLembrete não encontrado.')
        return
    debt = next((d for d in db.get_debts() if d['id'] == num), None)
    if debt is None:
        await q.edit_message_text(f'{original}\n\n✅ Essa conta já está quitada ou foi removida.')
        return
    if action == 'snz':
        due = now + timedelta(hours=1)
        db.insert_reminder(due.isoformat(), f'Pagar {debt["title"]}')
        await q.edit_message_text(f'{original}\n\n⏰ Vou lembrar de novo às {due.strftime("%H:%M")}.')
    elif action == 'pay':
        remaining = debt['original_amount'] - debt['paid_amount']
        nonce = secrets.token_hex(8)
        context.application.bot_data.setdefault('pending_payments', {})[nonce] = (num, remaining, now.timestamp())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Confirmar registro', callback_data='pay:' + nonce),
            InlineKeyboardButton('Cancelar', callback_data='cancel:' + nonce)]])
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(
            f'Registrar o pagamento TOTAL de {db.brl(remaining)} da conta “{debt["title"]}”?\nNão transfere dinheiro de verdade.',
            reply_markup=keyboard)


POLL_SECONDS = 15
BATTERY_CHECK_SECONDS = 300
_battery_next = 0.0
_battery_warned = False


async def battery_job(application):
    """Avisa quando a bateria cai de 20/10/5% (e quando chega a 100% carregando)."""
    global _battery_next, _battery_warned
    if not BATTERY_ALERTS or time.monotonic() < _battery_next:
        return
    _battery_next = time.monotonic() + BATTERY_CHECK_SECONDS
    info = await battery.read_status()
    if info is None:
        if not _battery_warned:
            log.info('Bateria indisponível (Termux:API ausente?). Alertas de bateria inativos.')
            _battery_warned = True
        return
    message, state = battery.decide(info['pct'], info['charging'], db.get_pref('battery_state', ''),
                                    BATTERY_LEVELS, BATTERY_FULL_ALERT)
    if message:
        await send_owner(application, message)
    db.set_pref('battery_state', state)


async def send_owner(application, text, markup=None):
    """Envia em HTML; se o Telegram recusar a formatação, reenvia como texto puro."""
    try:
        await application.bot.send_message(OWNER_ID, text, parse_mode='HTML', reply_markup=markup)
    except Exception as exc:
        if type(exc).__name__ != 'BadRequest':
            raise
        await application.bot.send_message(OWNER_ID, re.sub(r'</?[a-z]+>', '', html.unescape(text)), reply_markup=markup)


def reminder_keyboard(rid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('⏰ +1 hora', callback_data=f'rm:h1:{rid}'),
        InlineKeyboardButton('📅 Amanhã', callback_data=f'rm:dy:{rid}')]])


def debt_keyboard(debt_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Já paguei', callback_data=f'da:pay:{debt_id}'),
        InlineKeyboardButton('⏰ Lembrar em 1h', callback_data=f'da:snz:{debt_id}')]])


def reminder_text(reminder, now=None):
    text = f'⏰ <b>Lembrete #{reminder["id"]}</b>\n{html.escape(reminder["content"])}'
    late = ((now or db.now()) - datetime.fromisoformat(reminder['due_at'])).total_seconds()
    if late > 180:
        text += f'\n\n<i>(enviado com {int(late // 60)} min de atraso)</i>'
    return text


def debt_alert_text(debt):
    days = debt['days_left']
    title = html.escape(str(debt['title']))
    head = (f'📅 <b>Conta vencendo em {days} dias!</b>' if days >= 2 else
            '⚠️ <b>Conta vence amanhã!</b>' if days == 1 else
            '🔴 <b>Conta vence HOJE!</b>' if days == 0 else '❗ <b>Conta em atraso!</b>')
    due = db.short_date(debt['due_date'])
    tail = f'🗓 Venceu em: {due} ({-days} dia{"s" if days < -1 else ""} atrás)' if days < 0 else f'🗓 Vencimento: {due}'
    return f'{head}\n📌 {title}\n💳 Restante: {html.escape(db.brl(debt["remaining"]))}\n{tail}'


async def send_due_reminders(application):
    for reminder in db.due_reminders():
        try:
            await send_owner(application, reminder_text(reminder), reminder_keyboard(reminder['id']))
            db.mark_reminder(reminder['id'])
        except Exception as exc:
            log.warning('Falha de envio do lembrete #%s: %s', reminder['id'], type(exc).__name__)


async def send_debt_alerts(application):
    if db.now().hour < MORNING_HOUR:
        return  # alertas de conta só a partir do horário do resumo matinal
    for debt in db.get_debts_due_soon():
        if db.was_debt_notified(debt['id'], debt['alert_type']):
            continue
        try:
            await send_owner(application, debt_alert_text(debt), debt_keyboard(debt['id']))
            db.mark_debt_notified(debt['id'], debt['alert_type'])
        except Exception as exc:
            log.warning('Falha ao enviar alerta de dívida #%s: %s', debt['id'], type(exc).__name__)


async def reminder_worker(application):
    """Cada etapa é isolada: um erro ou lentidão em uma não atrasa lembretes."""
    while True:
        if OWNER_ID:
            for step in (send_due_reminders, send_debt_alerts, battery_job, daily_jobs):
                try:
                    await step(application)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning('Falha na etapa %s: %s', step.__name__, type(exc).__name__)
            try:
                db.generate_recurring()
            except Exception:
                log.exception('Falha ao gerar contas fixas')
        await asyncio.sleep(POLL_SECONDS)


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
    setup_file_logging()
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
    app=(ApplicationBuilder().token(TOKEN).post_init(on_init).post_stop(on_stop)
         .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(30)
         .get_updates_connect_timeout(20).get_updates_read_timeout(40).build())
    app.add_error_handler(on_error)
    routes={'id':show_id,'start':start,'ajuda':help_cmd,'menu':menu_cmd,'receita':movement,'despesa':movement,
            'saldo':balance,'conta':debt_add,'dividas':debt_list,'pagar':pay,'historico':history,
            'relatorio':report,'planejar':plan,'lembrete':reminder_add,'hora':clock_cmd,'bateria':battery_cmd,
            'lembretes':reminder_list,'apagar_lembrete':reminder_delete,
            'modelo':mode_set,'backup':backup,'resetar':reset_db_cmd,
            'corrigir_conta':debt_edit,'excluir_conta':debt_delete,'apagar':tx_delete,'estornar':tx_delete,
            'fixa':recurring_add,'fixas':recurring_list,'excluir_fixa':recurring_delete}
    for name, handler in routes.items():
        app.add_handler(CommandHandler(name,handler))
    app.add_handler(CallbackQueryHandler(payment_callback,pattern='^(pay|cancel):[a-f0-9]{16}$'))
    app.add_handler(CallbackQueryHandler(natural_action_callback,pattern='^nl:(confirm|cancel):[a-f0-9]{16}$'))
    app.add_handler(CallbackQueryHandler(alert_callback,pattern='^(rm:(h1|dy)|da:(pay|snz)):\\d+$'))
    app.add_handler(CallbackQueryHandler(mode_callback,pattern='^mode:(auto|rapido|avancado|gemini)$'))
    app.add_handler(CallbackQueryHandler(menu_callback,pattern='^mn:'))
    # block=False: enquanto a IA pensa, botões e comandos continuam funcionando
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,chat,block=False))
    log.info('Iniciando o bot em long polling...')
    app.run_polling(drop_pending_updates=False)


if __name__=='__main__':main()
