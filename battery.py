"""Leitura e alertas da bateria do celular via Termux:API (comando termux-battery-status)."""
import asyncio
import json
import logging
import os
import signal

log = logging.getLogger('financeai')
HELP_INSTALL = ('Para ler a bateria é preciso: 1) instalar o app Termux:API (mesma fonte do Termux, F-Droid); '
                '2) no Termux, rodar: pkg install termux-api')


def parse_status(text):
    """Converte a saída JSON do termux-battery-status em dict; ValueError se inválida."""
    try:
        data = json.loads(text)
        pct = int(data['percentage'])
    except (ValueError, TypeError, KeyError):
        raise ValueError('Saída inválida do termux-battery-status') from None
    status = str(data.get('status', '')).upper()
    plugged = str(data.get('plugged', 'UNPLUGGED')).upper()
    return {'pct': max(0, min(100, pct)),
            'charging': plugged not in ('UNPLUGGED', '') or status in ('CHARGING', 'FULL'),
            'status': status, 'health': str(data.get('health', '')).upper(),
            'temperature': data.get('temperature')}


async def read_status(timeout=10):
    """Retorna o dict de parse_status, ou None se indisponível (sem Termux:API, erro ou tempo excedido)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'termux-battery-status', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return parse_status(out.decode('utf-8', 'ignore'))
    except (asyncio.TimeoutError, ValueError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # mata também os processos-filhos que seguram o pipe
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()
        return None


def decide(pct, charging, state, levels, full_alert=True):
    """Decide o aviso. Retorna (mensagem|None, novo_estado). Estados: '', 'low:<nivel>', 'full'."""
    if charging:
        if pct >= 100 and full_alert:
            if state == 'full':
                return None, state
            return '🔋 Bateria em 100%. Pode tirar do carregador.', 'full'
        return None, ''
    if state == 'full':
        state = ''
    below = [lv for lv in levels if pct <= lv]
    if not below:
        return None, ''
    level = min(below)
    if state.startswith('low:') and int(state[4:]) <= level:
        return None, state
    icon = '🪫' if level > 5 else '🚨'
    return (f'{icon} Bateria em {pct}%. Conecte o carregador: se o celular desligar, '
            'o bot para e você perde lembretes e alertas.'), f'low:{level}'


def describe(info):
    state = 'carregando' if info['charging'] else 'descarregando'
    lines = [f'🔋 Bateria: {info["pct"]}% ({state})']
    if info.get('temperature') is not None:
        lines.append(f'🌡 Temperatura: {info["temperature"]} °C')
    if info.get('health'):
        lines.append(f'❤️ Saúde: {info["health"].capitalize()}')
    return '\n'.join(lines)
