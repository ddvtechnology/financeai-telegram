# FinanceAI Telegram — assistente por linguagem natural v2

Projeto novo para uso pessoal no Telegram, com banco SQLite local no aparelho. A interação principal é **conversar normalmente**; comandos não são necessários para o uso diário. Os botões são apenas atalhos.

## Como você conversa com ele

- `Gastei R$ 45,90 no almoço.`
- `Recebi R$ 1.870 de salário.`
- `Tenho uma conta de luz de R$ 120 para pagar dia 28.`
- `Paguei R$ 100 do aluguel.`
- `Me lembra de pagar o aluguel dia 28 às 9h.`
- `Quanto gastei este mês?`
- `Quais contas ainda faltam pagar?`
- `Me ajude a organizar meu orçamento.`

Para receitas, despesas, contas, pagamentos de dívidas e lembretes, o bot apresenta um resumo e só grava depois que você toca em **Confirmar registro**. Se faltar valor, categoria ou data, ele pergunta antes. Cancelar não grava nada.

Os atalhos do Telegram são: **Saldo**, **Registrar receita**, **Registrar despesa**, **Contas a pagar** e **Relatório**. Também é possível digitar tudo com suas próprias palavras. O bot mantém consultas de saldo, dívidas, histórico, relatório, planejamento e lembretes.

## O que fica salvo

- Receitas e despesas, com valores inteiros em centavos para evitar erros de arredondamento.
- Contas a pagar e pagamentos parciais, sem contar uma conta futura como despesa antes do pagamento registrado.
- Lembretes, histórico, resumos mensais, CSV e backup SQLite.
- Conversas recentes usadas como contexto da IA.

O saldo é o resultado dos registros deste bot — **não é o saldo real de uma conta bancária**. O bot não acessa bancos, não faz PIX e não transfere dinheiro. Confirmar um pagamento significa apenas registrar no SQLite que você informou tê-lo pago.

## 1. Criar o bot no Telegram

1. Abra o [BotFather oficial](https://t.me/BotFather) e envie `/newbot`.
2. Escolha nome e username terminando em `bot`.
3. Guarde o token recebido em local privado. Não o envie a ninguém.
4. Abra a conversa privada com seu bot.

Se um token vazar, use `/revoke` no BotFather e substitua-o no `.env`.

## 2. Chaves NVIDIA

1. Acesse [NVIDIA Build](https://build.nvidia.com/models), autentique-se e gere uma chave com acesso aos modelos configurados.
2. Coloque a chave em `NVIDIA_KEY_LIGHTNING` e `NVIDIA_KEY_ULTRA`. Uma chave pode ser usada nas duas entradas se tiver acesso a ambos os modelos.
3. Modelos: `nvidia/nemotron-3.5-lightning-30b-a3b` e `nvidia/nemotron-3-ultra-550b-a55b`.

As chamadas de interpretação e as perguntas livres enviam a mensagem ao serviço NVIDIA. Para perguntas financeiras, o bot também pode enviar um resumo das movimentações e contas cadastradas. Revise os termos de privacidade e não envie senhas, tokens, números completos de cartão ou outras credenciais.

## 3. Instalar no Termux (Redmi Note 12)

Use Termux de uma fonte oficial (F-Droid ou GitHub). Baixe `financeai-telegram-natural-v2.zip` para a pasta **Downloads** do celular. No Termux, execute:

```bash
pkg update && pkg upgrade -y
pkg install -y python python-pip python-ensurepip-wheels unzip nano
termux-setup-storage
cd ~
unzip ~/storage/downloads/financeai-telegram-natural-v2.zip
cd ~/financeai-telegram-zero
bash install-termux.sh
```

Aceite a permissão de armazenamento quando o Android pedir. O instalador cria um ambiente Python e instala as dependências, inclusive a base de fusos horários.

## 4. Configurar o token e o ID do Telegram

Edite o arquivo:

```bash
nano .env
```

Preencha primeiro apenas `TELEGRAM_BOT_TOKEN`. Para salvar no Nano: **Ctrl+O**, **Enter**, **Ctrl+X**. Se não aparecer `File Name to Write`, veja a mensagem na parte inferior da tela antes de pressionar Enter.

Inicie:

```bash
./run.sh
```

No chat privado do seu bot, envie `/id` e anote o número. Volte ao Termux e pare o bot com `Ctrl+C`. Edite `nano .env` novamente e preencha os quatro valores:

```ini
TELEGRAM_BOT_TOKEN=SEU_TOKEN_DO_BOTFATHER
TELEGRAM_OWNER_ID=SEU_ID_NUMERICO
NVIDIA_KEY_LIGHTNING=SUA_CHAVE_NVIDIA
NVIDIA_KEY_ULTRA=SUA_CHAVE_NVIDIA
TIMEZONE=America/Recife
```

Não envie esses dados aqui no ChatGPT. Salve e saia do Nano, depois restrinja as permissões e inicie:

```bash
chmod 600 .env
./run.sh
```

No Telegram, envie `/start`. A tela apresentará os botões de atalho. Experimente escrever `Gastei R$ 45,90 no almoço`; confira o resumo e confirme somente se os dados estiverem corretos.

## 5. Comandos opcionais

A interação diária não depende de comandos. Estes continuam disponíveis como alternativas:

- `/id` — mostra o ID do Telegram no chat privado, para configurar o proprietário.
- `/start` e `/ajuda` — apresentação e exemplos.
- `/receita 1870 Salário | Mensal` e `/despesa 45,90 Alimentação | Almoço` — registros estruturados antigos.
- `/saldo`, `/conta`, `/dividas`, `/pagar`, `/historico`, `/relatorio`, `/planejar`, `/lembrete`, `/lembretes`, `/modelo` e `/backup` — atalhos administrativos.

Para manter a segurança, apenas o ID definido em `TELEGRAM_OWNER_ID` pode usar os recursos financeiros e somente em conversa privada. `/id` fica disponível antes dessa configuração.

## 6. Manter o bot ativo

O bot precisa que o Termux permaneça ativo e com internet. Para separar a sessão do terminal, pode-se usar `tmux`:

```bash
tmux new -s financeai
./run.sh
```

Para sair sem encerrar o processo, pressione `Ctrl+B`, solte e depois pressione `D`. Para retornar: `tmux attach -t financeai`.

A inicialização automática com Termux:Boot é opcional e não garante funcionamento contínuo: o Android pode encerrar apps em segundo plano. Proteja o aparelho, monitore aquecimento e faça backups externos.

## 7. Segurança e limites

- As receitas, despesas, dívidas e lembretes ficam em `data/financeai.sqlite3`.
- O SQLite não é criptografado por padrão. Proteja o celular e os backups.
- O Telegram comum com bots não é um chat secreto com criptografia ponta a ponta.
- A IA pode interpretar incorretamente uma frase. **Confira sempre a tela de confirmação antes de salvar**; nunca confirme um valor, categoria ou vencimento incorreto.
- Não há leitura automática de áudio, imagens ou comprovantes nesta versão.
- Lembretes são enviados quando o bot está ativo e conectado; não são garantia de alarme se o Android encerrar o Termux.

## Arquivos

```text
financeai-telegram-zero/
├── bot.py
├── ai.py
├── db.py
├── config.py
├── requirements.txt
├── install-termux.sh
├── run.sh
├── data/
└── logs/
```
