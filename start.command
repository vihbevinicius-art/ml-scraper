#!/bin/bash

# Resolve a pasta do projeto:
# - Se o script está dentro da pasta do projeto, usa o diretório dele.
# - Senão (ex: foi colocado no Desktop), cai pra ~/Desktop/ml-mensagens.
SCRIPT_DIR="$( cd "$( dirname "$0" )" && pwd )"
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    cd "$SCRIPT_DIR"
elif [ -f "$HOME/Desktop/ml-mensagens/requirements.txt" ]; then
    cd "$HOME/Desktop/ml-mensagens"
else
    echo "❌ Não encontrei a pasta do projeto (requirements.txt)."
    echo "   Esperado: ~/Desktop/ml-mensagens/"
    echo ""
    echo "Pressione Enter para fechar..."
    read
    exit 1
fi

echo "📂 Pasta do projeto: $(pwd)"

# Cria o ambiente Python se ainda não existir
if [ ! -d ".venv" ]; then
    echo ""
    echo "🔧 Primeiro uso — criando ambiente Python (pode demorar ~1 min)..."
    python3 -m venv .venv || {
        echo "❌ Falha ao criar o ambiente Python."
        echo "   Você precisa ter o Python 3 instalado."
        echo ""
        echo "Pressione Enter para fechar..."
        read
        exit 1
    }
fi

# Instala/atualiza dependências quando o requirements.txt mudou
if [ ! -f ".venv/.deps-installed" ] || [ "requirements.txt" -nt ".venv/.deps-installed" ]; then
    echo ""
    echo "📦 Instalando dependências..."
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
    touch .venv/.deps-installed
fi

# ─── Baixa cloudflared local (1ª vez só, ~25MB) ────────────────────────────
if [ ! -f "./cloudflared" ]; then
    echo ""
    echo "📥 Baixando cloudflared (compartilhar link público — 1x só)..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
        URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
    else
        URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    fi
    curl -sL "$URL" -o /tmp/cloudflared.tgz && tar -xzf /tmp/cloudflared.tgz -C ./ && chmod +x ./cloudflared
    rm -f /tmp/cloudflared.tgz
    if [ ! -f "./cloudflared" ]; then
        echo "⚠️  Falha ao baixar cloudflared. App vai rodar só localmente."
    fi
fi

# Lê (ou cria) a senha de acesso — Python gera na 1ª importação
.venv/bin/python -c "from main import SENHA_ACESSO; print(SENHA_ACESSO)" > /tmp/ml-senha.txt 2>&1
SENHA=$(cat .senha 2>/dev/null || tail -1 /tmp/ml-senha.txt)
rm -f /tmp/ml-senha.txt

# Sobe o uvicorn em background
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8765 > /tmp/ml-mensagens.log 2>&1 &
UVICORN_PID=$!

# Quando o script morrer, mata o uvicorn junto
trap "kill $UVICORN_PID 2>/dev/null; exit 0" EXIT INT TERM

# Espera o servidor responder antes de subir o tunnel
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null -u "x:$SENHA" http://localhost:8765/; then
        break
    fi
    sleep 0.5
done

# Abre o navegador local pra você usar normalmente
( sleep 1 && open "http://localhost:8765" ) &

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  🚀 Servidor local:  http://localhost:8765"
echo "  🔑 SENHA de acesso: $SENHA"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo ""

if [ -f "./cloudflared" ]; then
    echo "🌐 Criando link público — procure abaixo por uma linha tipo:"
    echo "   https://XXX-XXX-XXX.trycloudflare.com"
    echo ""
    echo "   Mande essa URL + a senha acima pro seu amigo 👆"
    echo "   Para encerrar tudo: feche esta janela ou Ctrl+C"
    echo "───────────────────────────────────────────────────────────────"
    echo ""
    # cloudflared em foreground — output streamado mostra a URL pública
    ./cloudflared tunnel --url http://localhost:8765 --no-autoupdate
else
    echo "ℹ️  Sem link público (cloudflared falhou no download)."
    echo "   App rodando local em http://localhost:8765"
    echo "   Ctrl+C para encerrar."
    wait $UVICORN_PID
fi
