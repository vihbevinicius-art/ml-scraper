#!/bin/bash
# Sobe o projeto pro GitHub automaticamente.
# Roda 1x só (depois pra atualizar é só rodar de novo).

SCRIPT_DIR="$( cd "$( dirname "$0" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  📤 SUBIR APP PRO GITHUB (passo único)"
echo "════════════════════════════════════════════════════════════════"
echo ""

# 1) Verifica se gh está autenticado
if ! gh auth status &> /dev/null; then
    echo "1️⃣  Primeiro passo: vamos te logar no GitHub."
    echo ""
    echo "   Aperte ENTER pra começar — vai aparecer um CÓDIGO tipo 'A1B2-C3D4'."
    echo "   Você COPIA esse código e cola no navegador (vai abrir sozinho)."
    echo ""
    read -p "   Aperte ENTER pra começar..."
    echo ""
    # Pré-seleciona github.com + https + browser pra reduzir prompts
    gh auth login --hostname github.com --git-protocol https --web
    if ! gh auth status &> /dev/null; then
        echo ""
        echo "❌ Login não terminou. Roda de novo este script quando estiver pronto."
        read -p "Enter pra fechar..."
        exit 1
    fi
fi

USER=$(gh api user --jq .login)
echo ""
echo "✅ Logado como: $USER"
echo ""

# 2) Cria/atualiza o repositório
REPO_NAME="ml-scraper"
REPO_FULL="$USER/$REPO_NAME"

if gh repo view "$REPO_FULL" &> /dev/null; then
    echo "2️⃣  Repositório '$REPO_FULL' já existe. Atualizando..."
    git remote remove origin 2> /dev/null
    git remote add origin "https://github.com/$REPO_FULL.git"
    git add .
    git -c user.email="$USER@users.noreply.github.com" -c user.name="$USER" commit -m "Atualizar app" 2> /dev/null
    git push -u origin main --force
else
    echo "2️⃣  Criando repositório '$REPO_FULL' (público)..."
    gh repo create "$REPO_NAME" --public --source=. --remote=origin --push
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ✅ TUDO PRONTO NO GITHUB!"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  Seu repositório: https://github.com/$REPO_FULL"
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  PRÓXIMO PASSO: subir no Render (vou te guiar)"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  1. Abra https://render.com e clique 'Get Started' (login com GitHub)."
echo "  2. No painel, clique 'New +' → 'Web Service'."
echo "  3. Conecte o GitHub e selecione o repo '$REPO_NAME'."
echo "  4. O Render lê o arquivo render.yaml — quase tudo já vem configurado."
echo "  5. Em 'Environment', adicione:"
echo "       Key:   SENHA_ACESSO"
echo "       Value: rio-tigre-kiwi-neve675"
echo "     (ou outra senha que você queira — anota pra mandar pro amigo)"
echo "  6. Clique 'Deploy Web Service' e espera ~5min."
echo "  7. Vai aparecer uma URL tipo: https://$REPO_NAME-XXXX.onrender.com"
echo "  8. Manda essa URL + senha pro seu amigo. Pronto!"
echo ""
read -p "Aperte ENTER pra fechar..."
