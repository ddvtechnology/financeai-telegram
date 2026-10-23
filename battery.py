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


def _env():
    """Garante que os binários do Termux estejam no PATH (ex.: bot iniciado por Termux:Boot ou tmux)."""
    env = dict(os.environ)
    prefix = env.get('PREFIX') or '/data/data/com.termux/files/usr'
    bin_dir = os.path.join(prefix, 'bin')
    if os.path.isdir(bin_dir) and bin_dir not in env.get('PATH', '').split(os.pathsep):
        env['PATH'] = bin_dir + os.pathsep + env.get('PATH', '')
    return env


async def check(timeout=10):
    """Lê a bateria. Retorna (info, None) ou (None, motivo) com motivo 'missing', 'timeout' ou 'invalid:<saída>'."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'termux-battery-status', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, env=_env())
    except (FileNotFoundError, PermissionError, OSError):
        return None, 'missing'
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # mata também os processos-filhos que seguram o pipe
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()
        return None, 'timeout'
    text = out.decode('utf-8', 'ignore').strip()
    try:
        return parse_status(text), None
    except ValueError:
        return None, 'invalid:' + (text[:120] or '(vazia)')


async def read_status(timeout=10):
    """Retorna o dict de parse_status, ou None se indisponível."""
    info, _ = await check(timeout)
    return info


def explain(reason):
    """Mensagem de ajuda conforme o motivo da falha."""
    if reason == 'missing':
        return ('O comando termux-battery-status não existe. No Termux, rode:\npkg install termux-api')
    if reason == 'timeout':
        return ('O comando não obteve resposta em 10 s. Isso quase sempre significa que o app Termux:API '
                'não está instalado, nunca foi aberto, veio de uma fonte diferente do Termux (F-Droid x GitHub) '
                'ou o Android/MIUI o está bloqueando. Abra o app Termux:API uma vez, libere a inicialização '
                'automática e deixe a bateria dele em "Sem restrições".')
    if str(reason).startswith('invalid:'):
        return 'O comando respondeu algo inesperado: ' + str(reason)[8:]
    return HELP_INSTALL


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
