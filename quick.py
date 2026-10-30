"""Leitura determinística de frases simples como "Recebi 831 do complemento do salário".

Serve de reserva quando a IA demora, falha ou responde fora do formato: o lançamento
ainda vira um botão de confirmação, em vez de cair no chat livre (que pode inventar).
"""
import re
import unicodedata
from datetime import timedelta

_VERBS = {'recebi': 'receita', 'ganhei': 'receita', 'gastei': 'despesa', 'paguei': 'despesa', 'comprei': 'despesa'}
_START = re.compile(r'^\s*(?:eu\s+)?(recebi|ganhei|gastei|paguei|comprei)\b\s*(.*)$', re.I | re.S)
_AMOUNT = re.compile(r'(?:r\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[,.]\d{1,2})?)(?:\s*reais\b)?', re.I)
_LEAD = re.compile(r'^(?:de|do|da|dos|das|com|no|na|em|pelo|pela|por|pra|para)\s+', re.I)


_EXPENSE_CATEGORIES = (
    ('Alimentação', ('almoco', 'jantar', 'lanche', 'cafe', 'padaria', 'mercado', 'supermercado',
                     'feira', 'restaurante', 'ifood', 'pizza', 'comida', 'acougue', 'hortifruti')),
    ('Transporte', ('uber', '99', 'taxi', 'onibus', 'metro', 'passagem', 'combustivel',
                    'gasolina', 'etanol', 'estacionamento', 'pedagio', 'oficina', 'moto', 'carro')),
    ('Moradia', ('aluguel', 'condominio', 'luz', 'energia', 'agua', 'internet', 'gas', 'iptu', 'casa', 'reforma')),
    ('Saúde', ('farmacia', 'remedio', 'medico', 'consulta', 'exame', 'dentista', 'hospital', 'academia')),
    ('Educação', ('faculdade', 'curso', 'escola', 'livro', 'apostila', 'mensalidade')),
    ('Lazer', ('cinema', 'show', 'viagem', 'passeio', 'bar', 'cerveja', 'streaming', 'netflix', 'spotify', 'jogo')),
    ('Vestuário', ('roupa', 'camisa', 'calca', 'sapato', 'tenis')),
    ('Cuidados Pessoais', ('cabelo', 'barbearia', 'salao', 'perfume', 'cosmetico')),
)


def _plain(text):
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii').lower()


def _infer_category(kind, plain):
    words = set(re.findall(r'[a-z0-9]+', plain))
    if kind == 'receita':
        if words & {'salario', 'adiantamento', 'complemento', 'decimo', '13o', 'ferias', 'folha'}:
            return 'Salário'
        if words & {'freela', 'freelance', 'bico', 'extra', 'venda', 'comissao'}:
            return 'Renda Extra'
        return 'Receita'
    for cat, keywords in _EXPENSE_CATEGORIES:
        if words & set(keywords):
            return cat
    return 'Outros'


def parse(text, today):
    """Retorna dict no formato do interpretador (type=transaction) ou None se não for frase simples."""
    m = _START.match(str(text or ''))
    if not m:
        return None
    kind = _VERBS[m.group(1).lower()]
    rest = m.group(2)
    amounts = list(_AMOUNT.finditer(rest))
    if len(amounts) != 1:
        return None
    a = amounts[0]
    desc = (rest[:a.start()] + ' ' + rest[a.end():]).strip(' ,.-')
    desc = re.sub(r'\b(?:pode\s+colocar\s+data\s+de\s+hoje|hoje|ontem)\b', '', desc, flags=re.I)
    desc = re.sub(r'\s+', ' ', desc).strip(' ,.-')
    desc = _LEAD.sub('', desc).strip()
    plain = _plain(desc)
    category = _infer_category(kind, plain)
    day = today - timedelta(days=1) if re.search(r'\bontem\b', _plain(text)) else today
    return {'type': 'transaction', 'kind': kind, 'amount': a.group(1),
            'category': category, 'description': desc[:1].upper() + desc[1:] if desc else category,
            'occurred_date': day.isoformat()}


def is_record_attempt(text):
    """Frase em 1ª pessoa de registro (recebi/gastei... + número): nunca pode ir ao chat livre."""
    m = _START.match(str(text or ''))
    return bool(m and _AMOUNT.search(m.group(2)))
