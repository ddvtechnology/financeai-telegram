#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
pkg update -y
pkg install -y python python-pip nano tmux
pkg install -y termux-api || true  # opcional: alertas de bateria (precisa também do app Termux:API)
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
mkdir -p data logs
chmod 700 data logs
printf '\nInstalação concluída! Edite: nano .env\nRode: ./run.sh\n'
