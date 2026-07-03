import hashlib
import re
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _texto(el) -> Optional[str]:
    if not el:
        return None
    txt = el.get_text(" ", strip=True)
    return txt or None


def _primeiro(soup: BeautifulSoup, seletores):
    for sel in seletores:
        el = soup.select_one(sel)
        if el:
            return el
    return None


def _parse_preco(texto: Optional[str]) -> Optional[float]:
    if not texto:
        return None
    limpo = texto.replace("\xa0", " ").strip()
    # Captura uma sequência de dígitos com possíveis separadores
    m = re.search(r"\d[\d.,]*\d|\d", limpo)
    if not m:
        return None
    s = m.group(0)
    # Decide qual separador é decimal vs milhar
    if "," in s and "." in s:
        # O separador decimal é o que aparece por último
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # BR: 1.234,56 -> 1234.56
        else:
            s = s.replace(",", "")  # US: 1,234.56 -> 1234.56
    elif "," in s:
        # Só vírgula: decimal se 1-2 dígitos no final, senão milhar
        partes = s.rsplit(",", 1)
        if 1 <= len(partes[1]) <= 2:
            s = partes[0].replace(",", "") + "." + partes[1]
        else:
            s = s.replace(",", "")
    elif "." in s:
        # Só ponto: se a última seção tem 3 dígitos e há outra antes, é milhar BR
        partes = s.rsplit(".", 1)
        if len(partes[1]) == 3 and partes[0]:
            s = s.replace(".", "")
        # senão é decimal (US ou BR sem milhares), deixa
    try:
        return float(s)
    except ValueError:
        return None


def _extrair_preco_principal(bloco) -> Optional[float]:
    """Extrai o preço atual do bloco principal do produto. Pega fraction E
    cents do MESMO container .ui-pdp-price__second-line — sem isso, em produtos
    com preço redondo (sem centavos), pegava o cents do primeiro carrossel
    abaixo, montando preços Frankenstein (ex: 302 do produto + 56 de outro
    produto = 302,56)."""
    container = bloco.select_one("div.ui-pdp-price__second-line")
    if not container:
        return None
    fraction_el = container.select_one(".andes-money-amount__fraction")
    if not fraction_el:
        return None
    fraction = fraction_el.get_text(strip=True)
    cents_el = container.select_one(".andes-money-amount__cents")
    cents = cents_el.get_text(strip=True) if cents_el else "00"
    return _parse_preco(f"{fraction},{cents}")


def _bloco_preco_principal(soup: BeautifulSoup):
    """Retorna o subtree do produto PRINCIPAL para isolar a busca de preço
    de carrosséis e listas de recomendação que aparecem na mesma página."""
    h1 = soup.select_one("h1.ui-pdp-title")
    if not h1:
        return soup
    container = h1
    for _ in range(15):
        container = container.find_parent()
        if not container:
            break
        if container.select_one(".ui-pdp-price__main-container"):
            return container
    return soup


def _preco_de_money_amount(el) -> Optional[float]:
    """Lê preço de qualquer .andes-money-amount: prioriza aria-label,
    senão monta a partir de fraction + cents."""
    if not el:
        return None
    aria = el.get("aria-label", "")
    if aria:
        m = re.search(r"(\d[\d.]*)\s*reais?(?:\s*com\s*(\d{1,2})\s*centavos?)?", aria, re.IGNORECASE)
        if m:
            inteiro = m.group(1).replace(".", "")
            cents = m.group(2) or "00"
            return _parse_preco(f"{inteiro},{cents}")
    fraction = el.select_one(".andes-money-amount__fraction")
    if fraction:
        cents = el.select_one(".andes-money-amount__cents")
        bruto = fraction.get_text(strip=True)
        if cents:
            bruto += "," + cents.get_text(strip=True)
        return _parse_preco(bruto)
    return _parse_preco(_texto(el))


def _extrair_preco_original(soup: BeautifulSoup, preco_atual: Optional[float]):
    """Tenta vários seletores em sequência e retorna (preco, seletor_usado)."""
    bloco = _bloco_preco_principal(soup)

    tentativas = [
        ".s-price-details__original-value",
        ".ui-pdp-price__original-value",
        ".andes-money-amount--previous",
        "[class*='original'] .andes-money-amount",
        "[class*='previous'] .andes-money-amount",
        "s .andes-money-amount, del .andes-money-amount, s, del",
        "[aria-label*='Antes' i], [aria-label*='antes era' i], [aria-label*='preço original' i]",
        "[style*='line-through']",
    ]

    for sel in tentativas:
        for el in bloco.select(sel):
            preco = _preco_de_money_amount(el)
            if preco and (preco_atual is None or preco > preco_atual):
                return preco, sel

    return None, None


def _resolver_challenge_akamai(session: requests.Session, final_url: str) -> bool:
    raw = session.cookies.get("_bmstate")
    if not raw:
        return False

    decoded = urllib.parse.unquote(raw)
    partes = decoded.split(";")
    if len(partes) < 2:
        return False

    seed = partes[0]
    try:
        difficulty = int(partes[1])
    except ValueError:
        return False

    prefixo = "0" * difficulty
    i = 0
    if difficulty > 0:
        for i in range(0, 10_000_000):
            h = hashlib.sha256(f"{seed}{i}".encode()).hexdigest()
            if h.startswith(prefixo):
                break
        else:
            return False

    parsed = urllib.parse.urlparse(final_url)
    hostname = parsed.hostname or ""
    dot = hostname.find("mercadoli")
    domain = hostname[dot:] if dot != -1 else hostname
    if domain and not domain.startswith("."):
        domain = "." + domain

    bmc_value = urllib.parse.quote(f"{seed};{i}", safe="")
    session.cookies.set("_bmc", bmc_value, domain=domain, path="/")
    session.cookies.set("_bm_skipml", "true", domain=domain, path="/")
    return True


def _titulo_do_slug(url: str) -> Optional[str]:
    """Extrai um título a partir do slug da URL do produto. Ex:
    '.../microfone-hollyland-lark-a1-mini/p/MLB123' → 'Microfone Hollyland Lark A1 Mini'.
    Funciona sem carregar a página — só a URL basta."""
    m = re.search(r"mercadoli(?:vre|bre)\.com(?:\.br)?/([a-z0-9\-]+)/p/", url, re.I)
    if not m:
        m = re.search(r"/([a-z0-9\-]{8,})/p/MLB", url, re.I)
    if not m:
        return None
    slug = m.group(1)
    if slug.count("-") < 2:  # slug curto demais, provavelmente não é título
        return None
    titulo = slug.replace("-", " ").strip()
    # Capitaliza cada palavra (title case simples, preserva siglas curtas)
    palavras = [p.capitalize() if len(p) > 2 and not p.isdigit() else p
                for p in titulo.split()]
    return " ".join(palavras)


def _titulo_via_jina(url: str, timeout: int = 30) -> Optional[str]:
    """Usa o Jina Reader (r.jina.ai) pra ler a página do produto de um IP
    não bloqueado. Retorna o og:title (linha 'Title:' do markdown). Serve
    de fallback quando o ML bloqueia o scraping direto (ex: IP de data center).
    Retorna None se falhar ou vier título genérico ('Mercado Livre')."""
    try:
        r = requests.get(
            "https://r.jina.ai/" + url,
            headers={"x-timeout": str(timeout - 5), "Accept": "text/plain"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        m = re.search(r"^Title:\s*(.+)$", r.text, re.MULTILINE)
        if not m:
            return None
        titulo = m.group(1).strip()
        # Remove sufixos comuns
        titulo = re.sub(r"\s*[-|]\s*Mercado\s*Li[bv]re.*$", "", titulo, flags=re.I).strip()
        titulo = re.sub(r"\s*[-–]\s*R\$.*$", "", titulo).strip()
        # Rejeita títulos genéricos (página não era a do produto)
        generico = titulo.lower() in {
            "mercado livre", "mercado libre", "mercado livre brasil", "",
        }
        return None if generico else titulo
    except Exception:
        return None


def _buscar_pagina(url: str) -> requests.Response:
    import time

    session = requests.Session()
    session.headers.update(HEADERS)

    # Retry simples para 429 (rate limit do ML quando vários requests vêm seguidos)
    for tentativa in range(3):
        resp = session.get(url, timeout=20, allow_redirects=True)
        if resp.status_code == 429 and tentativa < 2:
            time.sleep(1.5 * (tentativa + 1))
            continue
        break
    resp.raise_for_status()

    # Após seguir todos os redirects (inclusive de links de afiliado), usa a URL
    # final para o retry — nunca a URL de afiliado original.
    final_url = resp.url

    # Página de challenge anti-bot do Akamai vem com ~10-15KB; página real
    # tem 200KB+. Threshold de 50KB cobre o challenge com folga e descarta
    # falsos positivos (página real nunca é tão pequena).
    if len(resp.text) < 50_000 and "verifyChallenge" in resp.text:
        if _resolver_challenge_akamai(session, final_url):
            resp = session.get(final_url, timeout=20, allow_redirects=True)
            resp.raise_for_status()

    return resp


def extrair_produto(url: str) -> dict:
    resultado = {
        "titulo": None,
        "descricao_curta": None,
        "preco_atual": None,
        "preco_original": None,
        "cupom": None,
        "frete_gratis": False,
        "url_original": url,
    }

    try:
        resp = _buscar_pagina(url)
    except requests.RequestException as e:
        resultado["erro"] = f"Falha no request: {e}"
        return resultado

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        resultado["erro"] = f"Falha no parser: {e}"
        return resultado

    # Links de afiliado às vezes apontam para vitrines /social/<loja>/ em vez
    # de uma página de produto. Nesse caso, segue o produto destacado da landing.
    if not soup.select_one("h1.ui-pdp-title") and "/social/" in resp.url:
        link_produto = None
        # 1º: card de destaque das landings de afiliado (selo "MAIS VENDIDO" etc)
        for sel in [".rl-card-featured a[href]", "[class*='card-featured'] a[href]"]:
            link_produto = soup.select_one(sel)
            if link_produto:
                break
        # 2º fallback: primeiro link de produto na ordem do HTML
        if not link_produto:
            link_produto = soup.select_one(
                "a[href*='/p/MLB'], a[href*='/up/MLBU'], a[href*='/MLB-']"
            )
        if link_produto and link_produto.get("href"):
            href = link_produto["href"]
            if href.startswith("/"):
                href = "https://www.mercadolivre.com.br" + href
            try:
                resp = _buscar_pagina(href)
                soup = BeautifulSoup(resp.text, "lxml")
            except requests.RequestException:
                pass

    titulo_el = _primeiro(
        soup,
        [
            "h1.ui-pdp-title",
            "h1.ui-pdp-component-title",
            "h1[class*='ui-pdp-title']",
            "h1",
        ],
    )
    resultado["titulo"] = _texto(titulo_el)

    # Fallback: se não pegou o h1 (página /social/ servida no fim, ou layout
    # diferente), usa og:title. ML formata como "Produto - R$ 246" — limpamos.
    if not resultado["titulo"]:
        og = soup.select_one("meta[property='og:title']")
        if og and og.get("content"):
            t = re.sub(r"\s*[-–]\s*R\$.*$", "", og["content"]).strip()
            if t:
                resultado["titulo"] = t

    # Detecta página de bloqueio anti-bot do ML. Comum quando o app roda em
    # IP de data center (ex: Render) — o ML serve uma página de verificação
    # ("Por segurança...") em vez do produto. Sinaliza pra cair no modo manual.
    if not soup.select_one("h1.ui-pdp-title"):
        title_tag = soup.select_one("title")
        title_low = title_tag.get_text(strip=True).lower() if title_tag else ""
        titulo_low = (resultado["titulo"] or "").lower()
        marcadores = (
            "por segurança", "para sua segurança", "antes de continuar",
            "verifique que", "não sou um robô", "no soy un robot",
            "acesso não autorizado", "unusual traffic",
        )
        combinado = f"{title_low} {titulo_low}"
        if any(m in combinado for m in marcadores):
            resultado["bloqueado"] = True
            resultado["titulo"] = None
            # Tenta recuperar o TÍTULO por fora do scraping bloqueado:
            # 1) Jina Reader (lê de um IP não bloqueado) — passa a URL original
            #    (afiliado/meli.la) pra ele renderizar a vitrine e pegar o og:title.
            titulo_ext = _titulo_via_jina(url)
            # 2) Fallback: slug da URL final resolvida (se for /p/MLB)
            if not titulo_ext:
                titulo_ext = _titulo_do_slug(resp.url)
            if titulo_ext:
                resultado["titulo"] = titulo_ext
            return resultado

    caracteristicas = []
    for li in soup.select("ul.ui-pdp-features li, ul.ui-vpp-highlighted-specs__features-list li"):
        t = _texto(li)
        if t:
            caracteristicas.append(t)
        if len(caracteristicas) >= 5:
            break

    if not caracteristicas:
        for sp in soup.select(".ui-pdp-highlighted-specs-res-features__list .ui-pdp-list__item, .ui-vpp-highlighted-specs__key-value"):
            t = _texto(sp)
            if t:
                caracteristicas.append(t)
            if len(caracteristicas) >= 5:
                break

    if not caracteristicas:
        desc_el = _primeiro(
            soup,
            [
                ".ui-pdp-description__content",
                "p.ui-pdp-description__content",
            ],
        )
        desc_txt = _texto(desc_el)
        if desc_txt:
            partes = [p.strip() for p in desc_txt.split("\n") if p.strip()]
            caracteristicas = partes[:5]

    resultado["descricao_curta"] = " | ".join(caracteristicas) if caracteristicas else None

    # Restringe a busca de preço ao bloco do produto principal — evita
    # contaminação de carrosséis ("Quem viu também viu", "Patrocinados", etc.)
    bloco_principal = _bloco_preco_principal(soup)
    preco_atual = _extrair_preco_principal(bloco_principal)

    if preco_atual is None:
        meta_price = soup.select_one("meta[itemprop='price']")
        if meta_price and meta_price.get("content"):
            preco_atual = _parse_preco(meta_price["content"])

    if preco_atual is None:
        el = _primeiro(
            bloco_principal,
            [
                ".ui-pdp-price__second-line .andes-money-amount",
                ".andes-money-amount.andes-money-amount--cents-superscript",
            ],
        )
        preco_atual = _parse_preco(_texto(el))

    resultado["preco_atual"] = preco_atual

    preco_original, seletor_origem = _extrair_preco_original(soup, preco_atual)
    resultado["preco_original"] = preco_original
    resultado["preco_original_seletor"] = seletor_origem

    # Frete grátis do produto principal — procura em seletores específicos do
    # buybox/shipping do ML, ignorando .poly-component__shipping (carrosséis).
    # Padrões comuns:
    #   "Frete grátis"               (incondicional — exibido no shipping-action)
    #   "FRETE GRÁTIS ACIMA DE R$ X" (condicional, na promotions-pill)
    resultado["frete_gratis"] = False
    for sel in [
        ".ui-pdp-shipping-action",  # call principal de frete do buybox
        ".ui-pdp-special-shipping-summary",  # pill "FRETE GRÁTIS ACIMA DE..."
        ".ui-pdp-color--GREEN.ui-pdp-family--REGULAR",  # texto verde "Frete grátis"
        ".ui-pdp-buybox .ui-pdp-promotions-pill-label",
    ]:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True).lower()
            if re.search(r"\b(?:frete|envio|chegar[áa])\s+gr[áa]tis\b", txt):
                resultado["frete_gratis"] = True
                break
        if resultado["frete_gratis"]:
            break

    cupom = None
    for sel in [
        ".ui-pdp-promotions-pill-label__text",
        ".ui-pdp-coupon__text",
        "[class*='coupon'] [class*='text']",
    ]:
        el = soup.select_one(sel)
        txt = _texto(el)
        if txt and ("cupom" in txt.lower() or "%" in txt or "off" in txt.lower()):
            cupom = txt
            break

    if not cupom:
        texto_pagina = soup.get_text(" ", strip=True)
        m = re.search(
            r"cupom[^.\n]{0,80}?(\d{1,3}\s?%|R\$\s?\d+(?:[.,]\d{2})?)",
            texto_pagina,
            re.IGNORECASE,
        )
        if m:
            cupom = m.group(0).strip()

    resultado["cupom"] = cupom

    return resultado


if __name__ == "__main__":
    import json
    import sys

    url_teste = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.mercadolivre.com.br/smartphone-samsung-galaxy-a55-5g-256gb-8gb-ram/p/MLB1040287843"
    )
    dados = extrair_produto(url_teste)
    print(json.dumps(dados, indent=2, ensure_ascii=False))
