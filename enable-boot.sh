#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/financeai" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$PWD"
nohup "$PWD/run.sh" >> "$PWD/logs/bot.log" 2>&1 &
EOF
chmod 700 "$HOME/.termux/boot/financeai"
echo 'Inicialização criada. Instale Termux:Boot da mesma origem do Termux e abra o aplicativo uma vez.'
