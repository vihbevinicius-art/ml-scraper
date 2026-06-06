import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import configuracoes
import historico
from scraper import extrair_produto
from scraper_aliexpress import extrair_ali_manual

_PROJETO_DIR = pathlib.Path(__file__).resolve().parent
_MARCAS_FILE = _PROJETO_DIR / "marcas.json"

# ─── Autenticação básica ────────────────────────────────────────────────────
# A senha é gerada na primeira execução e salva em .senha (texto plano).
# Você compartilha essa senha com quem quer dar acesso. Apaga o arquivo
# pra forçar nova senha. Usuário = qualquer coisa (ignorado).
_SENHA_FILE = _PROJETO_DIR / ".senha"


def _carregar_ou_criar_senha() -> str:
    """Resolve a senha de acesso, em ordem de preferência:
      1) variável de ambiente SENHA_ACESSO  (usada no Render/produção)
      2) arquivo .senha no projeto          (uso local; criado na 1ª vez)
      3) gera uma nova e salva em .senha    (fallback final)
    Em produção, sempre defina SENHA_ACESSO no painel do serviço — o disco
    no Render free não é persistente, então .senha não sobrevive a deploys."""
    env_senha = os.environ.get("SENHA_ACESSO", "").strip()
    if env_senha:
        return env_senha

    if _SENHA_FILE.exists():
        s = _SENHA_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s

    import random
    palavras = [
        "sol", "lua", "mar", "rio", "fogo", "vento", "neve", "azul",
        "verde", "rosa", "pera", "uva", "kiwi", "manga", "abacaxi",
        "morango", "limao", "praia", "monte", "vale", "tigre", "leao",
    ]
    senha = "-".join(random.sample(palavras, 3)) + str(random.randint(10, 99))
    try:
        _SENHA_FILE.write_text(senha, encoding="utf-8")
    except OSError:
        pass  # filesystem read-only (alguns ambientes serverless)
    return senha


SENHA_ACESSO = _carregar_ou_criar_senha()
_security = HTTPBasic()


def _verificar_acesso(credentials: HTTPBasicCredentials = Depends(_security)):
    """Valida a senha (constante-time pra evitar timing attacks).
    Usuário é ignorado — só a senha importa."""
    ok = secrets.compare_digest(credentials.password, SENHA_ACESSO)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Senha incorreta",
            headers={"WWW-Authenticate": 'Basic realm="ML Scraper"'},
        )
    return credentials.username


app = FastAPI(title="ML Scraper", dependencies=[Depends(_verificar_acesso)])

CABECALHO_ALIEXPRESS = "🛒 ACHADO NO ALIEXPRESS — VEM VER! 🛒"

# Tag do marketplace — vai na 1ª linha da mensagem pra identificar a origem
TAG_MARKETPLACE = {
    "ml": "[MERCADOLIVRE]",
    "aliexpress": "[ALIEXPRESS] - ESTOQUE NO BRASIL - SEM IMPOSTO",
}

# Palavras de marketing/SEO que costumam aparecer no final dos títulos
# e não acrescentam informação útil ao consumidor.
_TITULO_LIXO = {
    "original", "originais", "lacrado", "lacrada", "lacrados", "lacradas",
    "garantia", "garantida", "garantido",
    "novo", "nova", "novos", "novas",
    "exclusivo", "exclusiva", "exclusivos", "exclusivas",
    "lançamento", "lancamento",
    "oficial", "oficiais",
    "autêntico", "autentico", "autêntica", "autentica",
    "nota", "fiscal",
    "frete", "grátis", "gratis", "gratuito",
    "envio", "imediato", "rápido", "rapido",
    "pronta", "entrega",
    "promoção", "promocao", "desconto", "oferta", "imperdível", "imperdivel",
    "produto", "novinho", "zero", "novíssimo", "novissimo",
    "barato", "barata", "baratos", "baratas",
    "qualidade", "premium", "top",
}


def simplificar_titulo(titulo: Optional[str], max_chars: int = 70) -> Optional[str]:
    """Limpa títulos cheios de keyword stuffing — corta na primeira separação
    estrutural (vírgula, pipe, hífen), remove palavras de marketing nas pontas
    e limita o tamanho final preservando palavras inteiras."""
    if not titulo:
        return titulo

    t = titulo.strip()

    # 1) Corta no primeiro separador estrutural — em PT-BR, vírgula geralmente
    #    marca o início da listagem de specs/keywords
    m = re.search(r"\s*[,|;]\s+|\s+[-–—]\s+", t)
    if m:
        t = t[: m.start()].strip()

    # 2) Corta a partir da primeira palavra de marketing que aparecer
    #    depois da posição 3 — preserva marca/modelo no início (ex: "Ferramenta
    #    Bosch Original X" mantém o "Original"), mas remove o lixo SEO típico
    #    do final ("...Apple Original Lacrado Nf Garantia").
    palavras = t.split()
    norm = lambda p: re.sub(r"[^\wÀ-ÿ]", "", p).lower()
    for i, p in enumerate(palavras):
        if i >= 3 and norm(p) in _TITULO_LIXO:
            palavras = palavras[:i]
            break
    # Limpeza adicional das pontas (caso tenha sobrado lixo no início)
    while palavras and norm(palavras[0]) in _TITULO_LIXO:
        palavras.pop(0)
    t = " ".join(palavras).strip()

    # 3) Se ainda longo, corta em palavra inteira respeitando o limite
    if len(t) > max_chars:
        out = []
        for p in t.split():
            tentativa = (" ".join(out + [p])).strip()
            if len(tentativa) > max_chars and out:
                break
            out.append(p)
        t = " ".join(out)

    # 4) Normaliza espaços duplicados
    t = re.sub(r"\s+", " ", t).strip()
    return t or titulo  # se zerou, devolve original


# Regras de categoria — primeira regra que casar vence. Ordem importa:
# colocar categorias mais específicas (DRONE, GIMBAL) antes das genéricas.
# As palavras-chave são regex case-insensitive.
_CATEGORIAS_REGRAS = [
    ("DRONE", [
        r"\bdrone\b", r"\bmavic\b", r"\bphantom\b", r"\bavata\b",
        r"dji\s+air", r"dji\s+mini", r"dji\s+inspire", r"dji\s+neo",
    ]),
    ("GIMBAL", [
        r"\bgimbal\b", r"\bestabilizador\b", r"\bronin\b",
        r"osmo\s+mobile", r"osmo\s+pocket", r"smooth\s*\d",
    ]),
    ("LENTE", [
        r"\blente\b", r"\blens\b", r"objetiva", r"teleobjetiva",
        r"prime\s+lens", r"\bsigma\b", r"\btamron\b", r"\bsamyang\b",
        r"\brokinon\b", r"\bviltrox\b", r"\byongnuo\b",
        r"\b\d{2,3}mm\s*f", r"f/[12]\.\d", r"f\d\.\d",
        r"\b50\s?mm\b", r"\b35\s?mm\b", r"\b85\s?mm\b", r"\b24\s?mm\b",
        r"\b70-200\b", r"\b24-70\b", r"\b16-35\b", r"\b18-55\b",
    ]),
    ("ÁUDIO", [
        r"microfone", r"microphone", r"\bmic\b", r"lapela", r"lavalier",
        r"\bfone(?:s)?\b", r"headphone", r"headset", r"earbud",
        r"hollyland", r"\brode\b", r"dji\s*mic", r"saramonic", r"\bboya\b",
        r"shotgun", r"deadcat", r"gravador\s+(?:de\s+)?[áa]udio",
        r"zoom\s*h\d", r"\bmixer\b",
    ]),
    ("ILUMINAÇÃO", [
        r"softbox", r"ring\s*light", r"\bilumina[çc][ãa]o\b", r"lighting",
        r"luz\s+led", r"painel\s+led", r"led\s+panel", r"\bflash\b",
        r"speedlite", r"\bgodox\b", r"\baputure\b", r"\bnanlite\b",
        r"\bamaran\b", r"refletor\s+(?:de\s+)?(?:foto|v[íi]deo|est[úu]dio)",
    ]),
    ("CÂMERA", [
        r"\bc[âa]mera\b(?!\s+led|\s+de\s+vigil)",
        r"\bcamera\b(?!\s+led)", r"\bmirrorless\b", r"\bdslr\b",
        r"sony\s+a[\d]", r"alpha\s+a\d", r"canon\s+eos", r"nikon\s+z\d",
        r"fuji(?:film)?\s+x", r"\blumix\b", r"\bgopro\b", r"hero\s*\d+",
        r"\bzv-?\d", r"insta\s*360", r"pocket\s+cinema",
    ]),
    ("TRIPÉ", [
        r"trip[ée]", r"\btripod\b", r"monop[ée]", r"\bmonopod\b",
    ]),
    ("MEMÓRIA", [
        r"cart[ãa]o\s+(?:de\s+)?mem[óo]ria", r"sd\s*card", r"micro\s*sd",
        r"\bsandisk\b", r"\bkingston\b", r"\blexar\b", r"\bssd\b",
    ]),
    ("BATERIA", [
        r"\bbateria\b", r"\bbattery\b", r"carregador",
        r"power\s*bank", r"powerbank",
    ]),
]


# Primeira palavra do título → categoria. Em PT-BR, listings de afiliado quase
# sempre começam com o tipo do produto ("Bateria DJI...", "Lente Sigma..."),
# então isso tem prioridade sobre matches no meio do título.
_PRIMEIRA_PALAVRA_CATEGORIA = {
    "bateria": "BATERIA", "battery": "BATERIA", "carregador": "BATERIA",
    "powerbank": "BATERIA",
    "lente": "LENTE", "lens": "LENTE", "objetiva": "LENTE",
    "microfone": "ÁUDIO", "mic": "ÁUDIO", "lapela": "ÁUDIO",
    "fone": "ÁUDIO", "fones": "ÁUDIO", "headphone": "ÁUDIO",
    "drone": "DRONE",
    "câmera": "CÂMERA", "camera": "CÂMERA", "gopro": "CÂMERA",
    "tripé": "TRIPÉ", "tripe": "TRIPÉ", "tripod": "TRIPÉ", "monopé": "TRIPÉ",
    "gimbal": "GIMBAL", "estabilizador": "GIMBAL",
    "softbox": "ILUMINAÇÃO", "iluminação": "ILUMINAÇÃO", "iluminacao": "ILUMINAÇÃO",
    "flash": "ILUMINAÇÃO", "refletor": "ILUMINAÇÃO",
    "cartão": "MEMÓRIA", "cartao": "MEMÓRIA",
}


def detectar_categoria(titulo: Optional[str]) -> Optional[str]:
    """Classifica o título em uma categoria.
    Prioridade: (1) primeira palavra do título — em PT-BR o tipo do produto
    quase sempre vem no início. (2) busca por padrões em qualquer posição.
    Retorna a tag em maiúsculas (sem colchetes) ou None se não identificar."""
    if not titulo:
        return None
    # 1) Primeira palavra ganha — "Bateria DJI Air" é BATERIA, não DRONE
    palavras = titulo.strip().split()
    if palavras:
        primeira = re.sub(r"[^\wÀ-ÿ]", "", palavras[0]).lower()
        if primeira in _PRIMEIRA_PALAVRA_CATEGORIA:
            return _PRIMEIRA_PALAVRA_CATEGORIA[primeira]
    # 2) Fallback: regex em qualquer posição
    for cat, padroes in _CATEGORIAS_REGRAS:
        for p in padroes:
            if re.search(p, titulo, re.IGNORECASE):
                return cat
    return None


# ─── Guia de Produtos: classificação por marca ───────────────────────────
# Fonte única em marcas.json. As listas planas pra detecção são DERIVADAS
# do JSON (sem duplicação) — adicionar marca no JSON faz a detecção pegar
# automaticamente.
_GUIA_CACHE: Optional[dict] = None


def _nome_marca(s: str) -> str:
    """Tira qualificadores entre parênteses ('Ulanzi (linha básica)' → 'Ulanzi')
    pra usar como termo de busca no título."""
    return re.sub(r"\s*\(.*?\)\s*", "", s).strip()


def carregar_guia() -> dict:
    """Lê marcas.json e retorna:
       - 'guia': o JSON original (pra exibir na UI)
       - 'detect_verde': lista plana de marcas verdes (deduplicada)
       - 'detect_amarelo': marcas amarelas que NÃO estão em verde
       - 'detect_vermelho': palavras de bloqueio
    Cache em memória — rebuild se o arquivo mudar (mtime)."""
    global _GUIA_CACHE
    try:
        mtime = _MARCAS_FILE.stat().st_mtime
    except FileNotFoundError:
        return {"guia": {}, "detect_verde": [], "detect_amarelo": [], "detect_vermelho": []}

    if _GUIA_CACHE and _GUIA_CACHE.get("_mtime") == mtime:
        return _GUIA_CACHE

    data = json.loads(_MARCAS_FILE.read_text(encoding="utf-8"))

    verde_set = set()
    for cat in data.get("verde", []):
        for m in cat.get("marcas", []):
            verde_set.add(_nome_marca(m))

    amarelo_set = set()
    for cat in data.get("amarelo", []):
        for m in cat.get("marcas", []):
            nome = _nome_marca(m)
            if nome and nome not in verde_set:
                amarelo_set.add(nome)

    vermelho_palavras = list(
        data.get("vermelho", {}).get("palavras_bloqueio", [])
    )

    # Ordena por tamanho desc — marcas mais longas primeiro pra evitar matches
    # parciais ("DJI Mavic" deve ser checada antes de "DJI" puro, senão "DJI"
    # casa primeiro e perde a especificidade).
    detect_verde = sorted(verde_set, key=lambda s: (-len(s), s))
    detect_amarelo = sorted(amarelo_set, key=lambda s: (-len(s), s))

    _GUIA_CACHE = {
        "_mtime": mtime,
        "guia": data,
        "detect_verde": detect_verde,
        "detect_amarelo": detect_amarelo,
        "detect_vermelho": vermelho_palavras,
    }
    return _GUIA_CACHE


def classificar_marca(titulo: Optional[str]) -> dict:
    """Classifica o produto em verde/amarelo/vermelho conforme guia de marcas.
    Regras (nessa ordem):
      1) título contém palavra de bloqueio → vermelho
      2) título contém marca verde         → verde (marca aprovada)
      3) título contém marca amarela       → amarelo (confirmar)
      4) nenhuma marca reconhecida         → amarelo (marca não reconhecida)
    Sempre devolve dict; nunca None — facilita a renderização do badge."""
    if not titulo:
        return {
            "cor": "amarelo",
            "label": "⚠️ Confirme antes de postar",
            "motivo": "sem título",
        }

    guia = carregar_guia()
    t = titulo.lower()

    # 1) Vermelho — palavras de bloqueio
    for palavra in guia["detect_vermelho"]:
        if palavra.lower() in t:
            return {
                "cor": "vermelho",
                "label": "❌ Não postar",
                "motivo": f'contém "{palavra}"',
            }

    # 2) Verde — marca aprovada (longest match first)
    for marca in guia["detect_verde"]:
        if marca.lower() in t:
            return {
                "cor": "verde",
                "label": "✅ Marca aprovada — pode postar",
                "marca": marca,
            }

    # 3) Amarelo — marca intermediária
    for marca in guia["detect_amarelo"]:
        if marca.lower() in t:
            return {
                "cor": "amarelo",
                "label": "⚠️ Confirme antes de postar (só com desconto bom)",
                "marca": marca,
            }

    # 4) Nenhuma marca reconhecida — amarelo de cautela
    return {
        "cor": "amarelo",
        "label": "⚠️ Marca não reconhecida — confirme antes de postar",
        "motivo": "nenhuma marca da lista bateu",
    }


def detectar_plataforma(url: str) -> Optional[str]:
    u = url.lower()
    if any(d in u for d in [
        "aliexpress.com", "aliexpress.us", "s.click.aliexpress",
        "a.aliexpress", "click.aliexpress",
    ]):
        return "aliexpress"
    if any(d in u for d in [
        "mercadolivre.com", "mercadolibre.com", "meli.la",
        "mlads.com", "ml.com",
    ]):
        return "ml"
    return None


class PedidoURL(BaseModel):
    url: str
    cabecalho: Optional[str] = None
    cupom_override: Optional[str] = None


class PedidoCopia(BaseModel):
    url: str
    titulo: Optional[str] = None
    preco: Optional[float] = None
    plataforma: Optional[str] = None


class PedidoVerificar(BaseModel):
    urls: List[str]


def _preco_br(v: Optional[float]) -> str:
    if v is None:
        return ""
    s = f"{v:,.2f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def escolher_cabecalho(plataforma: Optional[str], passado: Optional[str]) -> str:
    if plataforma == "aliexpress":
        return CABECALHO_ALIEXPRESS
    if passado:
        return passado
    import random
    lista = configuracoes.obter_cabecalhos()
    return random.choice(lista) if lista else "🔥 PRODUTOS ESPECIAIS DO DIA HOJE NO MERCADO LIVRE 🔥"


def montar_mensagem_whatsapp(dados: dict, cabecalho: str) -> str:
    # 1ª linha: tag do marketplace (identifica a origem de cara).
    # 2ª linha: URL com label "link:" — o WhatsApp ainda gera o preview porque
    # detecta a URL logo no começo da mensagem.
    url = dados.get("url_original", "")
    tag = TAG_MARKETPLACE.get(dados.get("plataforma"))
    linhas = []
    if tag:
        linhas.append(tag)
    linhas += [f"link: {url}" if url else "link:", "", cabecalho, ""]

    titulo = dados.get("titulo") or "(sem título)"
    # Prefixa com [CATEGORIA] quando tiver — bate o olho e já filtra interesse
    cat = (dados.get("categoria") or "").strip()
    if cat:
        titulo = f"[{cat.upper()}] {titulo}"
    linhas.append(titulo)
    linhas.append("")

    if dados.get("preco_original") is not None:
        linhas.append(f"❌ Antes: R$ {_preco_br(dados['preco_original'])}")

    if dados.get("preco_atual") is not None:
        linhas.append(f"✅ AGORA: R$ {_preco_br(dados['preco_atual'])}")

    if dados.get("frete_gratis"):
        linhas.append("🚚 Frete GRÁTIS")

    if dados.get("cupom"):
        linhas.append(f"🎟️ Cupom: {dados['cupom']}")

    return "\n".join(linhas)


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>ML Scraper</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f2f2f2; display: flex; justify-content: center;
           align-items: flex-start; min-height: 100vh; padding: 40px 16px; }
    .card { background: #fff; border-radius: 12px; padding: 32px;
            max-width: 760px; width: 100%; box-shadow: 0 2px 12px rgba(0,0,0,.1); }
    h1 { font-size: 22px; color: #333; margin-bottom: 8px; }
    p.sub { color: #666; font-size: 14px; margin-bottom: 20px; }
    label { font-size: 13px; font-weight: 600; color: #444; }
    .cupom-row { display: flex; gap: 8px; margin: 6px 0 16px; }
    .cupom-row input { flex: 1; border: 1px solid #ccc; border-radius: 8px;
                       padding: 10px 14px; font-size: 14px; outline: none;
                       transition: border .2s; margin: 0; }
    .cupom-row input:focus { border-color: #3483fa; }
    button.cupom-clear { background: #e0e0e0; color: #555; padding: 10px 14px;
                         font-size: 14px; border-radius: 8px; flex-shrink: 0; }
    button.cupom-clear:hover { background: #ccc; }

    textarea#urls { width: 100%; min-height: 140px; border: 1px solid #ccc;
                    border-radius: 8px; padding: 12px 14px; font-size: 14px;
                    font-family: inherit; margin: 6px 0 16px; outline: none;
                    transition: border .2s; resize: vertical; }
    textarea#urls:focus { border-color: #3483fa; }
    button { background: #3483fa; color: #fff; border: none; border-radius: 8px;
             padding: 11px 28px; font-size: 15px; font-weight: 600;
             cursor: pointer; transition: background .2s; }
    button:hover { background: #2968c8; }
    button:disabled { background: #aaa; cursor: not-allowed; }

    #resultado { margin-top: 28px; }
    .topbar { display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 16px; gap: 12px; flex-wrap: wrap; }
    #contador { font-size: 14px; color: #444; font-weight: 600; }
    #contador .ok { color: #00a650; }
    #contador .err { color: #e53; margin-left: 4px; }

    .produto { border: 1px solid #e3e3e3; border-radius: 10px; padding: 18px;
               margin-bottom: 14px; position: relative; transition: border-color .2s; }
    .produto[data-status="loading"] { border-left: 4px solid #3483fa; background: #fafbff; }
    .produto[data-status="ok"]      { border-left: 4px solid #00a650; }
    .produto[data-status="erro"]    { border-left: 4px solid #e53; background: #fff7f7; }
    .produto .badge { position: absolute; top: 14px; right: 14px; font-size: 18px; }
    .plat-badge { display: inline-block; font-size: 11px; font-weight: 700;
                  padding: 2px 8px; border-radius: 999px; margin-bottom: 6px;
                  letter-spacing: .3px; }
    .plat-ml { background: #e6f7ec; color: #0a7c33; }
    .plat-ali { background: #fff4d6; color: #8a6300; }

    .campo-edit { margin-top: 10px; }
    .campo-edit label { display: block; font-size: 11px; color: #777;
                        font-weight: 600; text-transform: uppercase;
                        letter-spacing: .4px; margin-bottom: 3px; }
    .campo-edit input.f { width: 100%; border: 1px solid #ddd; border-radius: 6px;
                          padding: 8px 10px; font-size: 14px; outline: none;
                          font-family: inherit; transition: border .15s;
                          background: #fff; margin: 0; }
    .campo-edit input.f:focus { border-color: #3483fa; }
    .campo-edit input.f-titulo { font-size: 15px; font-weight: 600; color: #222; }
    .campos-precos { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .campos-cat-titulo { display: grid; grid-template-columns: 130px 1fr; gap: 10px; }
    .campo-cat input { text-transform: uppercase; font-weight: 600; color: #3483fa; }
    .frete-toggle { display: flex; align-items: center; gap: 8px; margin-top: 10px;
                    padding: 8px 10px; background: #f5f9ff; border: 1px solid #d6e6fb;
                    border-radius: 6px; font-size: 13px; color: #333; cursor: pointer;
                    user-select: none; transition: background .15s; }
    .frete-toggle:hover { background: #ebf2fc; }
    .frete-toggle input { margin: 0; cursor: pointer; width: 16px; height: 16px;
                          accent-color: #3483fa; }
    .frete-toggle span { line-height: 1.3; }
    .manual-hint { background: #fff4d6; border-left: 4px solid #f0ad4e; border-radius: 6px;
                   padding: 9px 12px; margin: 8px 0 4px; font-size: 13px; color: #6b4a00;
                   line-height: 1.4; }
    .manual-hint b { color: #8a6300; }

    /* ─── Painel "Rotina do dia" ─── */
    .rotina { border: 1px solid #d6e6fb; border-radius: 10px; margin-bottom: 22px;
              background: #f5f9ff; overflow: hidden; }
    .rotina-head { display: flex; justify-content: space-between; align-items: center;
                   padding: 13px 16px; cursor: pointer; user-select: none; }
    .rotina-head:hover { background: #ebf2fc; }
    .rotina-titulo { font-weight: 700; color: #1c5fc4; font-size: 15px; }
    .rotina-seta { color: #3483fa; font-size: 12px; transition: transform .2s; }
    .rotina.fechada .rotina-seta { transform: rotate(-90deg); }
    .rotina.fechada .rotina-corpo { display: none; }
    .rotina-corpo { padding: 0 16px 16px; }
    .rotina-dica { background: #fff; border-radius: 8px; padding: 11px 13px;
                   font-size: 13.5px; color: #333; line-height: 1.5; margin-bottom: 14px;
                   border-left: 4px solid #3483fa; }
    .rotina-dica b { color: #1c5fc4; }
    .rotina-aviso { background: #fff8e8; border-left: 4px solid #f0ad4e; border-radius: 8px;
                    padding: 11px 13px; font-size: 13px; color: #6b4a00; line-height: 1.5;
                    margin-top: 14px; }
    .rotina-aviso b { color: #8a6300; }
    .rotina-cat { display: flex; align-items: flex-start; gap: 10px; padding: 9px 11px;
                  border-radius: 8px; margin-bottom: 7px; background: #fff;
                  border: 1px solid #e3edfb; transition: opacity .15s; }
    .rotina-cat input { margin: 2px 0 0; width: 18px; height: 18px; flex-shrink: 0;
                        cursor: pointer; accent-color: #00a650; }
    .rotina-cat label { cursor: pointer; flex: 1; }
    .rotina-cat .cat-nome { font-weight: 700; color: #222; font-size: 14px;
                            display: block; margin-bottom: 2px; }
    .rotina-cat .cat-termos { font-size: 12.5px; color: #777; line-height: 1.4; }
    .rotina-cat.feito { opacity: .5; }
    .rotina-cat.feito .cat-nome { text-decoration: line-through; }
    .rotina-progresso { font-size: 13px; font-weight: 700; color: #00a650;
                        margin-bottom: 12px; text-align: center; }
    .rotina-progresso.completo { color: #1c5fc4; }

    /* ─── Badge de marca no topo do card ─── */
    .marca-badge { display: block; font-weight: 700; padding: 10px 14px;
                   border-radius: 8px; margin-bottom: 12px; font-size: 14px;
                   border-left: 5px solid transparent; line-height: 1.35; }
    .marca-badge.verde    { background: #e6f7ec; color: #0a7c33;
                            border-left-color: #0a7c33; }
    .marca-badge.amarelo  { background: #fff4d6; color: #8a6300;
                            border-left-color: #f0ad4e; }
    .marca-badge.vermelho { background: #fde2e2; color: #a52323;
                            border-left-color: #d33; }
    .marca-badge .detalhe { font-weight: 500; font-size: 12px; opacity: .85;
                            margin-left: 6px; }

    /* ─── Tela "Guia de Produtos" ─── */
    .guia-bloco { border-radius: 10px; padding: 18px 20px; margin-bottom: 18px;
                  border-left: 6px solid transparent; }
    .guia-bloco.verde    { background: #e6f7ec; border-left-color: #0a7c33; }
    .guia-bloco.amarelo  { background: #fff4d6; border-left-color: #f0ad4e; }
    .guia-bloco.vermelho { background: #fde2e2; border-left-color: #d33; }
    .guia-bloco h2 { font-size: 17px; margin-bottom: 6px; }
    .guia-bloco.verde h2    { color: #0a7c33; }
    .guia-bloco.amarelo h2  { color: #8a6300; }
    .guia-bloco.vermelho h2 { color: #a52323; }
    .guia-bloco .subtitulo { font-size: 13px; color: #555; margin-bottom: 14px;
                             font-style: italic; }
    .guia-categoria { margin-bottom: 12px; }
    .guia-categoria h3 { font-size: 13px; color: #444; font-weight: 700;
                         text-transform: uppercase; letter-spacing: .4px;
                         margin-bottom: 4px; }
    .guia-categoria p { font-size: 13.5px; color: #222; line-height: 1.5; }
    .guia-categoria .marca-chip { display: inline-block; background: rgba(255,255,255,.6);
                                  padding: 2px 9px; border-radius: 999px; margin: 2px 3px 2px 0;
                                  font-size: 13px; }
    .guia-vermelho-item { font-size: 14px; color: #5d1818; margin-bottom: 6px;
                          padding-left: 18px; position: relative; }
    .guia-vermelho-item::before { content: "•"; position: absolute; left: 0;
                                  color: #d33; font-weight: 700; }
    .ali-warn { background: #fff4d6; border: 1px solid #f0c040; border-left: 4px solid #f0ad4e;
                border-radius: 6px; padding: 12px 14px; margin-top: 10px; font-size: 13px;
                color: #6b4a00; line-height: 1.45; }
    .ali-warn b { color: #8a6300; }
    .ali-warn .btn-desbloq { background: #f0ad4e; color: #fff; padding: 8px 16px;
                             font-size: 13px; margin-top: 8px; display: inline-block;
                             border: none; border-radius: 6px; cursor: pointer;
                             font-weight: 600; }
    .ali-warn .btn-desbloq:hover { background: #ec971f; }
    .ali-warn .btn-desbloq.aberto { background: #5cb85c; }
    .produto .titulo { font-size: 15px; font-weight: 600; color: #222;
                       margin-right: 30px; line-height: 1.35; }
    .produto .url-pequena { font-size: 12px; color: #888; margin-top: 6px;
                            word-break: break-all; }
    .produto .erro-msg { color: #c00; font-size: 13px; margin-top: 8px;
                         background: #fff0f0; padding: 8px 10px; border-radius: 4px; }

    textarea.msg { width: 100%; min-height: 200px; border: 2px solid #25d366;
                   border-radius: 8px; padding: 12px; font-size: 13.5px;
                   font-family: -apple-system, 'Segoe UI', sans-serif;
                   line-height: 1.5; background: #f0fff4; color: #222;
                   resize: vertical; outline: none; margin-top: 12px; }
    textarea.msg:focus { border-color: #128c7e; }

    button.copiar { background: #25d366; margin-top: 8px; width: 100%;
                    display: flex; justify-content: center; align-items: center; gap: 6px; }
    button.copiar:hover { background: #1ebe5a; }
    button.copiar.copiado { background: #128c7e; }
    button.copiar.tudo { width: auto; padding: 9px 18px; font-size: 14px; margin: 0; }

    .tabs { display: flex; gap: 4px; border-bottom: 2px solid #eee; margin-bottom: 24px; }
    .tab { background: none; color: #666; padding: 10px 18px; border-radius: 0;
           font-size: 14px; font-weight: 600; border-bottom: 2px solid transparent;
           margin-bottom: -2px; }
    .tab:hover { background: #f5f5f5; }
    .tab.ativa { color: #3483fa; border-bottom-color: #3483fa; background: none; }
    .vista { display: none; }
    .vista.ativa { display: block; }

    textarea#cfg-cabecalhos { width: 100%; min-height: 180px; border: 1px solid #ccc;
                              border-radius: 8px; padding: 12px 14px; font-size: 14px;
                              font-family: inherit; margin: 6px 0 12px;
                              outline: none; transition: border .2s; resize: vertical; }
    textarea#cfg-cabecalhos:focus { border-color: #3483fa; }
    .cfg-hint { font-size: 12px; color: #888; margin-bottom: 12px; }
    .cfg-acoes { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .cfg-ok { font-size: 13px; color: #00a650; font-weight: 600; display: none; }

    .aviso-repost { background: #fff8dc; border-left: 4px solid #f0ad4e;
                    padding: 8px 12px; border-radius: 4px; font-size: 13px;
                    color: #8a6d3b; margin-top: 10px; }

    .hist-item { padding: 12px 14px; border: 1px solid #eee; border-radius: 8px;
                 margin-bottom: 8px; }
    .hist-item .data { font-size: 12px; color: #888; margin-bottom: 4px; }
    .hist-item .titulo-h { font-size: 14px; color: #222; font-weight: 600;
                           margin-bottom: 4px; }
    .hist-item a { color: #3483fa; text-decoration: none; font-size: 12px;
                   word-break: break-all; }
    .hist-vazio { text-align: center; color: #888; padding: 32px; font-size: 14px; }
    .hist-topbar { display: flex; justify-content: space-between; align-items: center;
                   margin-bottom: 16px; gap: 12px; flex-wrap: wrap; }
    button.limpar { background: #e53; padding: 9px 18px; font-size: 14px; }
    button.limpar:hover { background: #c33; }

    .spinner-mini { display: inline-block; width: 14px; height: 14px;
                    border: 2px solid #3483fa; border-top: 2px solid transparent;
                    border-radius: 50%; animation: spin .7s linear infinite;
                    vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <datalist id="categorias-sugeridas">
    <option value="LENTE"></option>
    <option value="ÁUDIO"></option>
    <option value="ILUMINAÇÃO"></option>
    <option value="DRONE"></option>
    <option value="CÂMERA"></option>
    <option value="GIMBAL"></option>
    <option value="TRIPÉ"></option>
    <option value="MEMÓRIA"></option>
    <option value="BATERIA"></option>
    <option value="ACESSÓRIO"></option>
  </datalist>
  <div class="card">
    <h1>ML Scraper</h1>

    <div class="tabs">
      <button class="tab ativa" id="tab-gerar" onclick="trocarAba('gerar')">Gerar mensagens</button>
      <button class="tab" id="tab-guia"  onclick="trocarAba('guia')">📋 Guia de Produtos</button>
      <button class="tab" id="tab-hist"  onclick="trocarAba('hist')">🕘 Histórico</button>
      <button class="tab" id="tab-cfg"   onclick="trocarAba('cfg')">⚙️ Cabeçalhos</button>
    </div>

    <div id="vista-gerar" class="vista ativa">

      <div class="rotina" id="rotina">
        <div class="rotina-head" onclick="toggleRotina()">
          <span class="rotina-titulo">📅 Rotina do dia — o que pesquisar hoje</span>
          <span class="rotina-seta" id="rotina-seta">▼</span>
        </div>
        <div class="rotina-corpo" id="rotina-corpo">
          <div class="rotina-dica">
            💡 <b>Pesquise TODAS as categorias todo dia!</b> Não fique só no que já conhece —
            cada tipo de produto entra em promoção em dias diferentes. Vá marcando abaixo
            conforme for pesquisando cada uma no Mercado Livre. A lista zera todo dia. 😉
          </div>
          <div id="rotina-checklist"><div class="hist-vazio">Carregando…</div></div>
          <div class="rotina-aviso">
            ⚠️ <b>Antes de postar qualquer coisa:</b> olhe o selo colorido que aparece em cada
            produto. Só mande os <b>🟢 verdes</b> (marca boa) ou <b>🟡 amarelos com desconto
            forte</b>. Os <b>🔴 vermelhos</b> ou sem marca conhecida, <b>não poste</b>.
            Na dúvida, abra a aba <b>📋 Guia de Produtos</b>.
          </div>
        </div>
      </div>

      <p class="sub">Cole vários links do Mercado Livre, um por linha.</p>
      <label for="cupom-dia">🎟️ Cupom do dia (opcional)</label>
      <div class="cupom-row">
        <input id="cupom-dia" type="text" placeholder="Ex: FOTO10" autocomplete="off" />
        <button class="cupom-clear" onclick="document.getElementById('cupom-dia').value=''" title="Limpar cupom">✖</button>
      </div>
      <label for="urls">Links</label>
      <textarea id="urls" placeholder="https://www.mercadolivre.com.br/...&#10;https://meli.la/...&#10;https://www.mercadolivre.com.br/..."></textarea>
      <button id="btn" onclick="gerar()">Gerar mensagens</button>
      <div id="resultado"></div>
    </div>

    <div id="vista-cfg" class="vista">
      <p class="sub">Edite os cabeçalhos das mensagens — um por linha. Quando gerar vários produtos ao mesmo tempo, cada card recebe um cabeçalho diferente em sequência.</p>
      <label for="cfg-cabecalhos">Variações de cabeçalho</label>
      <textarea id="cfg-cabecalhos" placeholder="Carregando…"></textarea>
      <p class="cfg-hint">Dica: adicione quantas variações quiser. Elas serão usadas em rotação automática.</p>
      <div class="cfg-acoes">
        <button onclick="salvarCabecalhos()">Salvar</button>
        <button onclick="restaurarCabecalhos()" style="background:#888">Restaurar padrões</button>
        <span class="cfg-ok" id="cfg-ok">✅ Salvo!</span>
      </div>
    </div>

    <div id="vista-hist" class="vista">
      <div class="hist-topbar">
        <p class="sub" style="margin:0">Últimos 30 produtos copiados.</p>
        <button class="limpar" onclick="limparHistorico()">🗑️ Limpar histórico</button>
      </div>
      <div id="hist-lista"><div class="hist-vazio">Carregando…</div></div>
    </div>

    <div id="vista-guia" class="vista">
      <p class="sub">Referência rápida pra decidir se vale postar nos grupos.
      O app já marca automaticamente cada produto capturado com um selo verde/amarelo/vermelho —
      essa tela é só pra você conferir as listas de marcas.</p>
      <div id="guia-conteudo"><div class="hist-vazio">Carregando…</div></div>
    </div>
  </div>

  <script>
    function escapeHtml(s) {
      return String(s ?? '').replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    // Busca cabeçalhos do servidor e monta sequência sem repetição seguida
    async function buscarCabecalhos() {
      try {
        const r = await fetch('/configuracoes/cabecalhos');
        if (r.ok) return await r.json();
      } catch (_) {}
      return ["🔥 PRODUTOS ESPECIAIS DO DIA HOJE NO MERCADO LIVRE 🔥"];
    }

    function montarSequencia(cabecalhos, total) {
      if (!cabecalhos.length) return Array(total).fill('');
      if (total === 1) {
        const idx = Math.floor(Math.random() * cabecalhos.length);
        return [cabecalhos[idx]];
      }
      // Rotação por índice — garante que nunca dois iguais seguidos
      // (desde que haja >= 2 cabeçalhos)
      return Array.from({length: total}, (_, i) => cabecalhos[i % cabecalhos.length]);
    }

    async function gerar() {
      const raw = document.getElementById('urls').value;
      const urls = raw.split('\\n').map(l => l.trim()).filter(l => l && /^https?:\\/\\//i.test(l));
      if (!urls.length) return;

      const btn = document.getElementById('btn');
      btn.disabled = true;
      btn.textContent = 'Gerando…';

      // Carrega cabeçalhos e pré-checa histórico em paralelo
      const [cabecalhos, recentesResp] = await Promise.all([
        buscarCabecalhos(),
        fetch('/historico/verificar', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({urls})
        }).then(r => r.ok ? r.json() : {}).catch(() => ({}))
      ]);
      const sequencia = montarSequencia(cabecalhos, urls.length);
      let recentes = recentesResp || {};

      const cont = document.getElementById('resultado');
      cont.innerHTML = `
        <div class="topbar">
          <div id="contador">Processando ${urls.length} link${urls.length>1?'s':''}…</div>
          <button id="btn-copiar-tudo" class="copiar tudo" onclick="copiarTudo()" disabled>📋 Copiar tudo</button>
        </div>
        <div id="cards"></div>`;

      const cards = document.getElementById('cards');
      urls.forEach((u, i) => {
        cards.insertAdjacentHTML('beforeend', `
          <div class="produto" id="card-${i}" data-status="loading">
            <div class="badge"><span class="spinner-mini"></span></div>
            <div class="titulo">Carregando…</div>
            <div class="url-pequena">${escapeHtml(u)}</div>
          </div>`);
      });

      window._resultados = new Array(urls.length);

      await Promise.all(urls.map(async (url, i) => {
        try {
          const cupomDia = document.getElementById('cupom-dia').value.trim();
          const r = await fetch('/extrair', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              url,
              cabecalho: sequencia[i],
              ...(cupomDia && {cupom_override: cupomDia})
            })
          });
          const d = await r.json();
          if (d.erro || !d.mensagem_whatsapp) {
            window._resultados[i] = {
              ok: false, url,
              erro: d.erro || 'Não foi possível extrair os dados',
              captcha_ali: d.erro === 'captcha_aliexpress',
              chrome_aberto: d.erro === 'chrome_aberto',
            };
          } else {
            window._resultados[i] = { ok: true, url, ...d, repost: recentes[url] || null };
          }
        } catch (e) {
          window._resultados[i] = { ok: false, url, erro: 'Erro de conexão: ' + e.message };
        }
        renderCard(i, window._resultados[i]);
        atualizarContador();
      }));

      btn.disabled = false;
      btn.textContent = 'Gerar mensagens';
      const okCount = window._resultados.filter(r => r && r.ok).length;
      document.getElementById('btn-copiar-tudo').disabled = okCount === 0;
    }

    function avisoRepost(repost) {
      if (!repost || !repost.copiado_em) return '';
      const dt = new Date(repost.copiado_em);
      const dias = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 86400000));
      const dataBR = dt.toLocaleDateString('pt-BR');
      const quando = dias === 0 ? 'hoje' : (dias === 1 ? 'há 1 dia' : `há ${dias} dias`);
      return `<div class="aviso-repost">⚠️ Você já postou esse produto ${quando} (${dataBR})</div>`;
    }

    function badgePlataforma(plat) {
      if (plat === 'aliexpress') return '<span class="plat-badge plat-ali">🟡 AliExpress</span>';
      if (plat === 'ml')         return '<span class="plat-badge plat-ml">🟢 Mercado Livre</span>';
      return '';
    }

    function parsePreco(txt) {
      if (txt == null) return null;
      const limpo = String(txt).replace(/[^\\d.,]/g, '');
      if (!limpo) return null;
      let s = limpo;
      if (s.includes(',') && s.includes('.')) {
        if (s.lastIndexOf(',') > s.lastIndexOf('.')) {
          s = s.replace(/\\./g, '').replace(',', '.');
        } else {
          s = s.replace(/,/g, '');
        }
      } else if (s.includes(',')) {
        const parts = s.split(',');
        if (parts[parts.length - 1].length === 2) s = s.replace(',', '.');
        else s = s.replace(/,/g, '');
      }
      const n = parseFloat(s);
      return isNaN(n) ? null : n;
    }

    function formatPrecoBR(v) {
      if (v == null) return '';
      return v.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    const TAGS_MARKETPLACE = {
      ml: '[MERCADOLIVRE]',
      aliexpress: '[ALIEXPRESS] - ESTOQUE NO BRASIL - SEM IMPOSTO',
    };

    function gerarMensagem(plataforma, cabecalho, categoria, titulo, precoOrig, precoAtual, freteGratis, cupom, urlOriginal) {
      // 1ª linha: tag do marketplace. 2ª linha: URL com label "link:".
      // O WhatsApp ainda gera preview porque a URL fica logo no começo.
      const url = urlOriginal || '';
      const linhaUrl = url ? `link: ${url}` : 'link:';
      const linhas = [];
      const tag = TAGS_MARKETPLACE[plataforma];
      if (tag) linhas.push(tag);
      linhas.push(linhaUrl, '', cabecalho, '');
      // Prefixa título com [CATEGORIA] quando tiver
      const tituloLimpo = (titulo || '(sem título)').trim();
      const cat = (categoria || '').trim().toUpperCase();
      linhas.push(cat ? `[${cat}] ${tituloLimpo}` : tituloLimpo);
      linhas.push('');
      if (precoOrig != null)  linhas.push(`❌ Antes: R$ ${formatPrecoBR(precoOrig)}`);
      if (precoAtual != null) linhas.push(`✅ AGORA: R$ ${formatPrecoBR(precoAtual)}`);
      if (freteGratis)        linhas.push('🚚 Frete GRÁTIS');
      if (cupom)              linhas.push(`🎟️ Cupom: ${cupom}`);
      return linhas.join('\\n');
    }

    function atualizarMensagem(i) {
      const r = window._resultados[i];
      if (!r) return;
      const cat    = document.getElementById('f-cat-' + i).value.trim();
      const titulo = document.getElementById('f-titulo-' + i).value.trim();
      const pAtual = parsePreco(document.getElementById('f-atual-' + i).value);
      const pOrig  = parsePreco(document.getElementById('f-orig-' + i).value);
      const cupom  = document.getElementById('f-cupom-' + i).value.trim();
      const frete  = document.getElementById('f-frete-' + i).checked;
      const msg = gerarMensagem(r.plataforma, r.cabecalho, cat, titulo, pOrig, pAtual, frete, cupom, r.url_original);
      document.getElementById('msg-' + i).value = msg;
      r.categoria = cat || null;
      r.titulo = titulo;
      r.preco_atual = pAtual;
      r.preco_original = pOrig;
      r.cupom = cupom || null;
      r.frete_gratis = frete;
      r.mensagem_whatsapp = msg;
    }

    function renderCard(i, r) {
      const el = document.getElementById('card-' + i);
      if (r.ok) {
        el.dataset.status = 'ok';
        const tituloVal = escapeHtml(r.titulo || '');
        const atualVal  = r.preco_atual != null    ? formatPrecoBR(r.preco_atual)    : '';
        const origVal   = r.preco_original != null ? formatPrecoBR(r.preco_original) : '';
        const cupomVal  = escapeHtml(r.cupom || '');
        const catVal    = escapeHtml(r.categoria || '');
        const freteChk  = r.frete_gratis ? 'checked' : '';
        const avisoManual = r.preencher_manual
          ? '<div class="manual-hint">✏️ <b>AliExpress:</b> preencha o preço e os demais campos abaixo. A mensagem se monta sozinha.</div>'
          : '';
        // Badge de marca (verde / amarelo / vermelho) — topo do card
        const bm = r.badge_marca || {};
        let badgeMarca = '';
        if (bm.cor) {
          const detalhe = bm.marca
            ? `<span class="detalhe">(marca: ${escapeHtml(bm.marca)})</span>`
            : (bm.motivo ? `<span class="detalhe">(${escapeHtml(bm.motivo)})</span>` : '');
          badgeMarca = `<div class="marca-badge ${bm.cor}">${escapeHtml(bm.label || '')} ${detalhe}</div>`;
        }
        el.innerHTML = `
          <div class="badge">${r.preencher_manual ? '✏️' : '✅'}</div>
          ${badgePlataforma(r.plataforma)}
          ${badgeMarca}
          ${avisoManual}
          <div class="campos-cat-titulo">
            <div class="campo-edit campo-cat">
              <label>Categoria</label>
              <input id="f-cat-${i}" class="f f-cat" list="categorias-sugeridas"
                     value="${catVal}" placeholder="(sem tag)"
                     oninput="atualizarMensagem(${i})" />
            </div>
            <div class="campo-edit campo-titulo">
              <label>Título</label>
              <input id="f-titulo-${i}" class="f f-titulo" value="${tituloVal}" oninput="atualizarMensagem(${i})" />
            </div>
          </div>
          <div class="campos-precos">
            <div class="campo-edit">
              <label>Antes (R$)</label>
              <input id="f-orig-${i}" class="f" value="${origVal}" placeholder="(sem desconto)" oninput="atualizarMensagem(${i})" />
            </div>
            <div class="campo-edit">
              <label>Agora (R$)</label>
              <input id="f-atual-${i}" class="f" value="${atualVal}" placeholder="(preencha)" oninput="atualizarMensagem(${i})" />
            </div>
          </div>
          <div class="campo-edit">
            <label>Cupom</label>
            <input id="f-cupom-${i}" class="f" value="${cupomVal}" placeholder="(sem cupom)" oninput="atualizarMensagem(${i})" />
          </div>
          <label class="frete-toggle">
            <input id="f-frete-${i}" type="checkbox" ${freteChk} onchange="atualizarMensagem(${i})" />
            <span>🚚 Incluir <b>Frete GRÁTIS</b> na mensagem</span>
          </label>
          ${avisoRepost(r.repost)}
          <textarea class="msg" id="msg-${i}" readonly>${escapeHtml(r.mensagem_whatsapp)}</textarea>
          <button id="btn-copiar-${i}" class="copiar" onclick="copiarItem(${i})">📋 Copiar</button>`;
      } else if (r.chrome_aberto) {
        el.dataset.status = 'erro';
        el.innerHTML = `
          <div class="badge">⚠️</div>
          <div class="titulo">Chrome do desbloqueio ainda está aberto</div>
          <div class="url-pequena">${escapeHtml(r.url)}</div>
          <div class="ali-warn">
            <b>Feche a janela do Chrome</b> que abriu pra resolver o captcha,
            depois tente gerar de novo. Se você fechou e ainda dá esse erro,
            clique abaixo pra forçar o encerramento.
            <br/>
            <button class="btn-desbloq" onclick="forcarFecharChrome(this)" style="background:#e53;">🛑 Forçar fechamento do Chrome</button>
          </div>`;
      } else if (r.captcha_ali) {
        el.dataset.status = 'erro';
        el.innerHTML = `
          <div class="badge">🤖</div>
          <div class="titulo">Captcha do AliExpress ativo</div>
          <div class="url-pequena">${escapeHtml(r.url)}</div>
          <div class="ali-warn">
            O AliExpress está pedindo verificação anti-robô antes de mostrar o produto.
            <br/>Clique abaixo, <b>arraste o slider</b> que aparecer e <b>feche a janela</b>.
            Depois é só tentar de novo — o desbloqueio dura horas/dias.
            <br/>
            <button class="btn-desbloq" onclick="desbloquearAli(this)">🔓 Abrir Chrome pra resolver captcha</button>
          </div>`;
      } else {
        el.dataset.status = 'erro';
        el.innerHTML = `
          <div class="badge">❌</div>
          <div class="titulo">Erro ao processar</div>
          <div class="url-pequena">${escapeHtml(r.url)}</div>
          <div class="erro-msg">${escapeHtml(r.erro)}</div>`;
      }
    }

    async function desbloquearAli(btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Abrindo Chrome...';
      try {
        const r = await fetch('/aliexpress/desbloquear', { method: 'POST' });
        if (r.ok) {
          btn.classList.add('aberto');
          btn.textContent = '✅ Chrome aberto — resolva o captcha e feche a janela';
        } else {
          btn.textContent = '❌ Falhou — tente de novo';
          btn.disabled = false;
        }
      } catch (e) {
        btn.textContent = '❌ Erro de conexão';
        btn.disabled = false;
      }
    }

    async function forcarFecharChrome(btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Encerrando...';
      try {
        const r = await fetch('/aliexpress/forcar-fechar', { method: 'POST' });
        if (r.ok) {
          btn.textContent = '✅ Chrome encerrado — clique em Gerar mensagens de novo';
        } else {
          btn.textContent = '❌ Falhou';
          btn.disabled = false;
        }
      } catch (e) {
        btn.textContent = '❌ Erro de conexão';
        btn.disabled = false;
      }
    }

    function atualizarContador() {
      const total = window._resultados.length;
      const feitos = window._resultados.filter(r => r).length;
      const ok = window._resultados.filter(r => r && r.ok).length;
      const err = feitos - ok;
      const c = document.getElementById('contador');
      if (feitos < total) {
        c.innerHTML = `Processando… <span class="ok">${ok}</span> de ${total} prontos`;
      } else {
        const errSpan = err > 0 ? `<span class="err">(${err} com erro)</span>` : '';
        c.innerHTML = `<span class="ok">${ok}</span> de ${total} produtos gerados com sucesso ${errSpan}`;
      }
      const btnTudo = document.getElementById('btn-copiar-tudo');
      if (btnTudo) btnTudo.disabled = ok === 0;
    }

    function flashCopiado(btn, label = '✅ Copiado!') {
      const original = btn.textContent;
      btn.textContent = label;
      btn.classList.add('copiado');
      setTimeout(() => {
        btn.textContent = original;
        btn.classList.remove('copiado');
      }, 2000);
    }

    function registrarCopia(r) {
      if (!r || !r.ok) return;
      fetch('/historico/registrar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url: r.url_original || r.url,
          titulo: r.titulo,
          preco: r.preco_atual,
          plataforma: r.plataforma,
        })
      }).catch(() => {});
    }

    function copiarItem(i) {
      const ta = document.getElementById('msg-' + i);
      const btn = document.getElementById('btn-copiar-' + i);
      ta.select();
      navigator.clipboard.writeText(ta.value).then(() => {
        flashCopiado(btn);
        registrarCopia(window._resultados[i]);
      }).catch(() => document.execCommand('copy'));
    }

    function copiarTudo() {
      const okList = (window._resultados || []).filter(r => r && r.ok);
      if (!okList.length) return;
      const sep = '\\n\\n———————————————\\n\\n';
      const tudo = okList.map(r => r.mensagem_whatsapp).join(sep);
      const btn = document.getElementById('btn-copiar-tudo');
      navigator.clipboard.writeText(tudo).then(() => {
        flashCopiado(btn, '✅ Tudo copiado!');
        okList.forEach(registrarCopia);
      });
    }

    function trocarAba(qual) {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('ativa'));
      document.querySelectorAll('.vista').forEach(v => v.classList.remove('ativa'));
      document.getElementById('tab-' + qual).classList.add('ativa');
      document.getElementById('vista-' + qual).classList.add('ativa');
      if (qual === 'hist') carregarHistorico();
      if (qual === 'cfg')  carregarCfg();
      if (qual === 'guia') carregarGuia();
    }

    let _guiaCarregado = false;
    async function carregarGuia() {
      if (_guiaCarregado) return;  // cache: guia é estático no ciclo da sessão
      const cont = document.getElementById('guia-conteudo');
      try {
        const r = await fetch('/marcas');
        const g = await r.json();

        const renderCategoria = (cat) => {
          const chips = (cat.marcas || []).map(m =>
            `<span class="marca-chip">${escapeHtml(m)}</span>`
          ).join(' ');
          const desc = cat.descricao
            ? `<p style="font-size:13px;color:#555;font-style:italic;margin:2px 0 4px;">${escapeHtml(cat.descricao)}</p>`
            : '';
          return `
            <div class="guia-categoria">
              <h3>${escapeHtml(cat.categoria)}</h3>
              ${desc}
              <p>${chips || '<i style="color:#888">(sem marcas listadas — regra descritiva)</i>'}</p>
            </div>`;
        };

        const blocoVerde = `
          <div class="guia-bloco verde">
            <h2>✅ MANDAR SEMPRE</h2>
            <p class="subtitulo">Marcas premium / mais vendidas e bem avaliadas</p>
            ${(g.verde || []).map(renderCategoria).join('')}
          </div>`;

        const blocoAmarelo = `
          <div class="guia-bloco amarelo">
            <h2>⚠️ POSTAR SÓ COM DESCONTO BOM (acima de ~20%) ou avaliação alta</h2>
            ${(g.amarelo || []).map(renderCategoria).join('')}
          </div>`;

        const descricoes = (g.vermelho && g.vermelho.descricoes) || [];
        const blocoVermelho = `
          <div class="guia-bloco vermelho">
            <h2>❌ NUNCA POSTAR</h2>
            ${descricoes.map(d => `<div class="guia-vermelho-item">${escapeHtml(d)}</div>`).join('')}
          </div>`;

        cont.innerHTML = blocoVerde + blocoAmarelo + blocoVermelho;
        _guiaCarregado = true;
      } catch (e) {
        cont.innerHTML = '<div class="hist-vazio">Erro ao carregar guia.</div>';
      }
    }

    const CABECALHOS_PADRAO = [
      "🔥 PRODUTOS ESPECIAIS DO DIA HOJE NO MERCADO LIVRE 🔥",
      "📸 ACHADO DO DIA PRA QUEM AMA FOTOGRAFIA 📸",
      "⚡ PREÇO BAIXOU NO ML — CORRE! ⚡",
      "🎯 OFERTA IMPERDÍVEL DE HOJE NO MERCADO LIVRE 🎯",
      "💥 TÁ BARATO! OLHA ESSE PRODUTO 💥",
    ];

    async function carregarCfg() {
      try {
        const r = await fetch('/configuracoes/cabecalhos');
        if (r.ok) {
          const lista = await r.json();
          document.getElementById('cfg-cabecalhos').value = lista.join('\\n');
        }
      } catch (_) {
        document.getElementById('cfg-cabecalhos').value = CABECALHOS_PADRAO.join('\\n');
      }
    }

    async function salvarCabecalhos() {
      const linhas = document.getElementById('cfg-cabecalhos').value
        .split('\\n').map(l => l.trim()).filter(l => l);
      if (!linhas.length) return;
      try {
        const r = await fetch('/configuracoes/cabecalhos', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({cabecalhos: linhas})
        });
        if (r.ok) {
          const salvos = await r.json();
          document.getElementById('cfg-cabecalhos').value = salvos.join('\\n');
          const ok = document.getElementById('cfg-ok');
          ok.style.display = 'inline';
          setTimeout(() => { ok.style.display = 'none'; }, 2000);
        }
      } catch (_) { alert('Erro ao salvar'); }
    }

    function restaurarCabecalhos() {
      if (!confirm('Restaurar os cabeçalhos originais? As suas variações serão apagadas.')) return;
      document.getElementById('cfg-cabecalhos').value = CABECALHOS_PADRAO.join('\\n');
      salvarCabecalhos();
    }

    async function carregarHistorico() {
      const lista = document.getElementById('hist-lista');
      lista.innerHTML = '<div class="hist-vazio">Carregando…</div>';
      try {
        const r = await fetch('/historico/listar');
        const itens = await r.json();
        if (!itens.length) {
          lista.innerHTML = '<div class="hist-vazio">Nenhum produto copiado ainda.</div>';
          return;
        }
        lista.innerHTML = itens.map(it => {
          const dt = new Date(it.copiado_em);
          const dataBR = dt.toLocaleString('pt-BR', {
            day:'2-digit', month:'2-digit', year:'numeric',
            hour:'2-digit', minute:'2-digit'
          });
          const preco = (it.preco != null)
            ? ' — R$ ' + Number(it.preco).toFixed(2).replace('.',',')
            : '';
          return `
            <div class="hist-item">
              <div class="data">${dataBR}${preco}</div>
              ${badgePlataforma(it.plataforma)}
              <div class="titulo-h">${escapeHtml(it.titulo || '(sem título)')}</div>
              <a href="${escapeHtml(it.url)}" target="_blank">${escapeHtml(it.url)}</a>
            </div>`;
        }).join('');
      } catch (e) {
        lista.innerHTML = '<div class="hist-vazio">Erro ao carregar histórico.</div>';
      }
    }

    async function limparHistorico() {
      if (!confirm('Tem certeza que deseja apagar todo o histórico?')) return;
      try {
        await fetch('/historico', { method: 'DELETE' });
        carregarHistorico();
      } catch (e) {
        alert('Erro ao limpar histórico');
      }
    }

    // ─── Rotina do dia ───────────────────────────────────────────────
    function _hojeStr() {
      const d = new Date();
      return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
    }
    function _chaveRotina() { return 'rotina-feito-' + _hojeStr(); }

    function _lerFeitos() {
      try { return JSON.parse(localStorage.getItem(_chaveRotina()) || '[]'); }
      catch (_) { return []; }
    }
    function _salvarFeitos(arr) {
      // Limpa marcações de dias anteriores pra não acumular lixo
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (k && k.startsWith('rotina-feito-') && k !== _chaveRotina()) {
            localStorage.removeItem(k);
          }
        }
        localStorage.setItem(_chaveRotina(), JSON.stringify(arr));
      } catch (_) {}
    }

    function toggleRotina() {
      const r = document.getElementById('rotina');
      r.classList.toggle('fechada');
      try { localStorage.setItem('rotina-aberta', r.classList.contains('fechada') ? '0' : '1'); } catch (_) {}
    }

    function marcarRotina(nome) {
      let feitos = _lerFeitos();
      if (feitos.includes(nome)) feitos = feitos.filter(n => n !== nome);
      else feitos.push(nome);
      _salvarFeitos(feitos);
      renderRotinaEstado();
    }

    function renderRotinaEstado() {
      const feitos = _lerFeitos();
      const cards = document.querySelectorAll('#rotina-checklist .rotina-cat');
      let total = 0, ok = 0;
      cards.forEach(c => {
        total++;
        const nome = c.dataset.nome;
        const chk = c.querySelector('input');
        const marcado = feitos.includes(nome);
        chk.checked = marcado;
        c.classList.toggle('feito', marcado);
        if (marcado) ok++;
      });
      const prog = document.getElementById('rotina-progresso');
      if (prog) {
        if (ok >= total && total > 0) {
          prog.textContent = '🎉 Tudo pesquisado hoje! Mandou bem!';
          prog.classList.add('completo');
        } else {
          prog.textContent = `✅ ${ok} de ${total} categorias pesquisadas hoje`;
          prog.classList.remove('completo');
        }
      }
    }

    let _rotinaCarregada = false;
    async function carregarRotina() {
      if (_rotinaCarregada) { renderRotinaEstado(); return; }
      const cont = document.getElementById('rotina-checklist');
      try {
        const r = await fetch('/marcas');
        const g = await r.json();
        const verde = g.verde || [];
        const html = `<div class="rotina-progresso" id="rotina-progresso"></div>` +
          verde.map(cat => {
            const safe = escapeHtml(cat.categoria);
            // Sugere as primeiras marcas como termos de busca
            const termos = escapeHtml((cat.marcas || []).slice(0, 6).join(', '));
            return `
              <div class="rotina-cat" data-nome="${safe}">
                <input type="checkbox" style="pointer-events:none" tabindex="-1" />
                <label>
                  <span class="cat-nome">${safe}</span>
                  <span class="cat-termos">🔎 Busque por: ${termos}</span>
                </label>
              </div>`;
          }).join('');
        cont.innerHTML = html;
        // Listeners: clicar em qualquer parte do card alterna a marcação
        cont.querySelectorAll('.rotina-cat').forEach(c => {
          c.addEventListener('click', () => marcarRotina(c.dataset.nome));
        });
        _rotinaCarregada = true;
        renderRotinaEstado();
      } catch (e) {
        cont.innerHTML = '<div class="hist-vazio">Erro ao carregar a rotina.</div>';
      }
    }

    // Inicialização: restaura estado aberto/fechado e carrega o checklist
    (function initRotina() {
      try {
        if (localStorage.getItem('rotina-aberta') === '0') {
          document.getElementById('rotina').classList.add('fechada');
        }
      } catch (_) {}
      carregarRotina();
    })();
  </script>
</body>
</html>
"""


@app.post("/extrair")
def extrair(pedido: PedidoURL):
    if not pedido.url.startswith("http"):
        raise HTTPException(status_code=400, detail="URL inválida")

    plataforma = detectar_plataforma(pedido.url)
    if plataforma == "ml":
        dados = extrair_produto(pedido.url)
        dados["plataforma"] = "ml"
    elif plataforma == "aliexpress":
        # AliExpress: modo manual — só pega o título (best-effort), o resto
        # o usuário preenche no card. Evita o captcha/Playwright que travava.
        dados = extrair_ali_manual(pedido.url)
    else:
        return {
            "erro": "Plataforma não suportada",
            "url_original": pedido.url,
            "plataforma": None,
        }

    if "erro" not in dados:
        # Simplifica o título antes de exibir/usar — remove keyword stuffing
        if dados.get("titulo"):
            dados["titulo"] = simplificar_titulo(dados["titulo"])
        # Detecta categoria automaticamente — usuário pode editar no card
        dados["categoria"] = detectar_categoria(dados.get("titulo"))
        # Classifica marca contra o guia (verde/amarelo/vermelho)
        dados["badge_marca"] = classificar_marca(dados.get("titulo"))
        if pedido.cupom_override and pedido.cupom_override.strip():
            dados["cupom"] = pedido.cupom_override.strip()
        dados["cabecalho"] = escolher_cabecalho(dados.get("plataforma"), pedido.cabecalho)
        dados["mensagem_whatsapp"] = montar_mensagem_whatsapp(dados, dados["cabecalho"])
    return dados


@app.get("/configuracoes/cabecalhos")
def cfg_listar():
    return configuracoes.obter_cabecalhos()


class PedidoCabecalhos(BaseModel):
    cabecalhos: List[str]


@app.put("/configuracoes/cabecalhos")
def cfg_salvar(pedido: PedidoCabecalhos):
    return configuracoes.salvar_cabecalhos(pedido.cabecalhos)


@app.post("/historico/registrar")
def historico_registrar(pedido: PedidoCopia):
    return historico.registrar(pedido.url, pedido.titulo, pedido.preco, pedido.plataforma)


@app.get("/historico/listar")
def historico_listar():
    return historico.listar(30)


@app.delete("/historico")
def historico_limpar():
    historico.limpar()
    return {"ok": True}


@app.post("/historico/verificar")
def historico_verificar(pedido: PedidoVerificar):
    return historico.buscar_recentes(pedido.urls, dias=7)


@app.get("/marcas")
def listar_marcas():
    """Retorna o guia de marcas pra renderizar a tela 'Guia de Produtos'."""
    return carregar_guia()["guia"]


@app.post("/aliexpress/forcar-fechar")
def aliexpress_forcar_fechar():
    """Mata todos os processos Chrome que estiverem usando o profile do
    scraper. Usado quando o usuário clicou em 'desbloquear' mas a janela
    sumiu / abriu fora da tela / travou."""
    profile = pathlib.Path.home() / ".ml-mensagens-chrome-profile"
    try:
        subprocess.run(
            ["pkill", "-f", f"user-data-dir={profile}"],
            timeout=5, check=False,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/aliexpress/desbloquear")
def aliexpress_desbloquear():
    """Dispara o script que abre o Chrome VISÍVEL pro usuário resolver o
    captcha do AliExpress manualmente. Roda em background — o endpoint
    retorna imediatamente; o Chrome fica vivo até o usuário fechar.
    Antes de abrir, mata qualquer Chrome anterior do mesmo profile
    (evita duplicata invisível por trás da janela atual)."""
    script = _PROJETO_DIR / "desbloquear_ali.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail="Script de desbloqueio não encontrado")

    # Mata Chromes anteriores no mesmo profile (sem isso, abrem múltiplos)
    profile = pathlib.Path.home() / ".ml-mensagens-chrome-profile"
    try:
        subprocess.run(
            ["pkill", "-f", f"user-data-dir={profile}"],
            timeout=5, check=False,
        )
        # Dá tempo do Chrome liberar o profile
        import time as _time
        _time.sleep(0.8)
    except Exception:
        pass

    try:
        # Loga pra arquivo pra a gente conseguir debugar quando falhar
        log_file = open("/tmp/desbloquear_ali.log", "ab")
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(_PROJETO_DIR),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        return {"ok": True, "mensagem": "Chrome aberto — arraste o slider do captcha e feche a janela."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao abrir Chrome: {e}")
