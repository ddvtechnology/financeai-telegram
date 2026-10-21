#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")" || exit 1
# Evita que o Android suspenda o Termux (causa comum de lembretes atrasados)
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
if [ ! -x .venv/bin/python ]; then echo 'Execute antes: bash install-termux.sh'; exit 1; fi
while true; do
  .venv/bin/python bot.py
  status=$?
  if [ "$status" -eq 0 ]; then exit 0; fi
  echo "FinanceAI saiu com código $status. Reiniciando em 15 segundos..."
  sleep 15
done
