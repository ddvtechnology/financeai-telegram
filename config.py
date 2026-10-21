"""Configuração isolada; sem dependências compiladas para Termux."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DB_PATH = BASE / 'data' / 'financeai.sqlite3'
TZ_NAME = os.getenv('TIMEZONE', 'America/Recife')
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    # O instalador inclui tzdata; este fallback só evita erro obscuro na configuração.
    raise RuntimeError(f'Fuso horário inválido ou base tzdata ausente: {TZ_NAME}. Instale tzdata e confira TIMEZONE no .env.') from None
try:
    MORNING_HOUR = min(23, max(0, int(os.getenv('MORNING_HOUR', '8'))))
except ValueError:
    MORNING_HOUR = 8
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
OWNER = os.getenv('TELEGRAM_OWNER_ID', '').strip()
OWNER_ID = int(OWNER) if OWNER.isdigit() else 0
NVIDIA_URL = 'https://integrate.api.nvidia.com/v1/chat/completions'
MODELS = {
    'rapido': ('nvidia/nemotron-3.5-lightning-30b-a3b', os.getenv('NVIDIA_KEY_LIGHTNING', '').strip()),
    'avancado': ('nvidia/nemotron-3-ultra-550b-a55b', os.getenv('NVIDIA_KEY_ULTRA', '').strip()),
}
