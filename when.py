"""Interpretação determinística de data e hora em português (não depende da IA).

A IA erra aritmética de tempo ("daqui a 30 minutos", "9 da noite", "sexta") e fuso
horário. Aqui o texto do usuário é lido por regras; a IA fica apenas como reserva.
Quando o texto cita vários horários ou datas, vale a última menção (correções).
"""
import re
import unicodedata
from datetime import datetime, timedelta

NUMBERS = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6,
           'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10, 'quinze': 15, 'vinte': 20, 'trinta': 30}
MONTHS = {'janeiro': 1, 'jan': 1, 'fevereiro': 2, 'fev': 2, 'marco': 3, 'mar': 3, 'abril': 4, 'abr': 4,
          'maio': 5, 'mai': 5, 'junho': 6, 'jun': 6, 'julho': 7, 'jul': 7, 'agosto': 8, 'ago': 8,
          'setembro': 9, 'set': 9, 'outubro': 10, 'out': 10, 'novembro': 11, 'nov': 11,
          'dezembro': 12, 'dez': 12}
WEEKDAYS = {'segunda': 0, 'terca': 1, 'quarta': 2, 'quinta': 3, 'sexta': 4, 'sabado': 5, 'domingo': 6}

_NUM = r'\d+|' + '|'.join(NUMBERS)
_PREFIX = r'(?:daqui(?:\s+a)?|dentro\s+de|de\s+aqui\s+a|em)'
_HALF_AND_HALF = re.compile(rf'\b{_PREFIX}\s+uma\s+hora\s+e\s+meia\b')
_HALF = re.compile(rf'\b{_PREFIX}\s+meia\s+hora\b')
_REL = re.compile(rf'\b{_PREFIX}\s+({_NUM})\s*(?:h\s*(\d{{1,2}})\b|(min(?:uto)?s?|horas?|hs?|dias?)\b)')
_COLON = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')
_HOUR = re.compile(r'\b([01]?\d|2[0-3])\s*(?:h|horas?)\s*([0-5]\d)?\b')
_AS = re.compile(r'\bas\s+([01]?\d|2[0-3])\b(?!\s*(?:/|:|de\b|dias?\b|reais\b|h\b))')
_PERIOD_HOUR = re.compile(r'\b([01]?\d|2[0-3])\s+(?=(?:da|de|a|pela|na|do)?\s*(?:manha|tarde|noite|madrugada)\b)')
_NOON = re.compile(r'\bmeio\s+dia\b')
_MIDNIGHT = re.compile(r'\bmeia\s+noite\b')
_PERIOD = re.compile(r'^\s*(?:da|de|a|pela|na|do)?\s*(manha|tarde|noite|madrugada)\b')
_DATE_NUM = re.compile(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b')
_DATE_TXT = re.compile(rf'\b(\d{{1,2}})\s+de\s+({"|".join(MONTHS)})(?:\s+de\s+(\d{{4}}))?\b')
_DAY_N = re.compile(r'\bdia\s+(\d{1,2})\b(?!\s*(?:/|de\b))')
_WEEKDAY = re.compile(rf'\b({"|".join(WEEKDAYS)})(?:\s+feira)?\b')
_WORDS = {'after_tomorrow': re.compile(r'\bdepois\s+de\s+amanha\b'),
          'tomorrow': re.compile(r'(?<!depois de )\bamanha\b'),
          'today': re.compile(r'\bhoje\b')}


def _norm(text):
    t = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii').lower()
    t = re.sub(r'[^a-z0-9:/\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _num(token):
    return int(token) if token.isdigit() else NUMBERS[token]


def _relative(t):
    """Retorna (minutos|None, dias|None, texto sem as expressões relativas)."""
    minutes = days = None
    last = -1
    for rx, value in ((_HALF_AND_HALF, 90), (_HALF, 30)):
        for m in rx.finditer(t):
            if m.start() > last:
                minutes, days, last = value, None, m.start()
    for m in _REL.finditer(t):
        if m.start() <= last:
            continue
        n = _num(m.group(1))
        if m.group(2) is not None:
            minutes, days = n * 60 + int(m.group(2)), None
        else:
            unit = m.group(3)
            if unit.startswith('min'):
                minutes, days = n, None
            elif unit.startswith('dia'):
                minutes, days = None, n
            else:
                minutes, days = n * 60, None
        last = m.start()
    t = _HALF_AND_HALF.sub(' ', t)
    t = _HALF.sub(' ', t)
    t = _REL.sub(' ', t)
    return minutes, days, t


def _clock(t):
    """Último horário citado como (hora, minuto) ou None."""
    found = []
    for m in _COLON.finditer(t):
        found.append((m.end(), int(m.group(1)), int(m.group(2)), m.end()))
    for m in _HOUR.finditer(t):
        found.append((m.end(), int(m.group(1)), int(m.group(2) or 0), m.end()))
    for m in _AS.finditer(t):
        found.append((m.end(), int(m.group(1)), 0, m.end()))
    for m in _PERIOD_HOUR.finditer(t):
        found.append((m.end(), int(m.group(1)), 0, m.end(1)))
    for m in _NOON.finditer(t):
        found.append((m.end(), 12, 0, None))
    for m in _MIDNIGHT.finditer(t):
        found.append((m.end(), 0, 0, None))
    if not found:
        return None
    _, hour, minute, tail = max(found, key=lambda f: f[0])
    if tail is not None:
        period = _PERIOD.match(t[tail:tail + 25])
        if period:
            word = period.group(1)
            if word in ('tarde', 'noite') and hour < 12:
                hour += 12
            elif word == 'noite' and hour == 12:
                hour = 0
            elif word == 'madrugada' and hour == 12:
                hour = 0
    return hour, minute


def _date_marker(t):
    """Última referência de data: (tipo, dados) ou None."""
    found = []
    for kind, rx in _WORDS.items():
        for m in rx.finditer(t):
            found.append((m.start(), kind, None))
    for m in _WEEKDAY.finditer(t):
        found.append((m.start(), 'weekday', WEEKDAYS[m.group(1)]))
    for m in _DAY_N.finditer(t):
        found.append((m.start(), 'day_n', int(m.group(1))))
    for m in _DATE_NUM.finditer(t):
        found.append((m.start(), 'date', (int(m.group(1)), int(m.group(2)), m.group(3))))
    for m in _DATE_TXT.finditer(t):
        found.append((m.start(), 'date', (int(m.group(1)), MONTHS[m.group(2)], m.group(3))))
    if not found:
        return None
    _, kind, data = max(found, key=lambda f: f[0])
    return kind, data


def resolve(text, now):
    """Converte o texto em datetime futuro (ou data explícita) no fuso de `now`; None se não achar horário."""
    t = _norm(text)
    minutes, rel_days, t = _relative(t)
    if minutes is not None:
        return (now + timedelta(minutes=minutes)).replace(microsecond=0)
    clock = _clock(t)
    if clock is None:
        return None
    hour, minute = clock
    today = now.date()

    def at(day):
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)

    try:
        if rel_days is not None:
            return at(today + timedelta(days=rel_days))
        marker = _date_marker(t)
        if marker is None:
            dt = at(today)
            return dt if dt > now else at(today + timedelta(days=1))
        kind, data = marker
        if kind == 'today':
            return at(today)
        if kind == 'tomorrow':
            return at(today + timedelta(days=1))
        if kind == 'after_tomorrow':
            return at(today + timedelta(days=2))
        if kind == 'weekday':
            for i in range(8):
                day = today + timedelta(days=i)
                if day.weekday() == data and at(day) > now:
                    return at(day)
        if kind == 'day_n':
            year, month = today.year, today.month
            for _ in range(13):
                try:
                    dt = at(today.replace(year=year, month=month, day=data))
                    if dt > now:
                        return dt
                except ValueError:
                    pass
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            return None
        if kind == 'date':
            d, m, y = data
            if y:
                return at(today.replace(year=int(y) + 2000 if len(y) == 2 else int(y), month=m, day=d))
            dt = at(today.replace(month=m, day=d))
            return dt if dt > now else at(today.replace(year=today.year + 1, month=m, day=d))
    except ValueError:
        return None
    return None


def humanize(delta):
    """'daqui a 2 h 5 min' / 'daqui a 3 dias' para a mensagem de confirmação."""
    total = int(delta.total_seconds() // 60)
    if total < 1:
        return 'agora'
    days, rest = divmod(total, 1440)
    hours, minutes = divmod(rest, 60)
    if days >= 2:
        return f'daqui a {days} dias'
    parts = []
    if days:
        hours += 24
    if hours:
        parts.append(f'{hours} h')
    if minutes:
        parts.append(f'{minutes} min')
    return 'daqui a ' + ' '.join(parts)
