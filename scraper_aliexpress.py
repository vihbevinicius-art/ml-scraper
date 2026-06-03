import json
import pathlib
import re
import subprocess
from typing import Optional

import requests
from bs4 import BeautifulSoup

# Profile persistente do Chrome dedicado pro scraper. Mantém cookies que
# permitem passar pelo desafio anti-bot do AliExpress (x5sec/punish) sem
# precisar resolver a cada execução.
_PROFILE_DIR = pathlib.Path.home() / ".ml-mensagens-chrome-profile"

# Script de stealth aplicado antes de qualquer JS da página. Mascara as
# principais flags que o WAF do AliExpress usa pra detectar automação.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, app: {} };
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }]
});
Object.defineProperty(navigator, 'languages', {
  get: () => ['pt-BR','pt','en-US','en']
});
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _parse_preco(texto: Optional[str]) -> Optional[float]:
    """Aceita formatos BR (1.234,56) e US (1,234.56). Remove qualquer símbolo
    monetário antes da extração."""
    if not texto:
        return None
    limpo = re.sub(r"[^\d.,]", "", texto.replace("\xa0", " "))
    if not limpo:
        return None
    # Formato com vírgula E ponto: assume que o último separador é o decimal
    if "," in limpo and "." in limpo:
        if limpo.rfind(",") > limpo.rfind("."):
            limpo = limpo.replace(".", "").replace(",", ".")
        else:
            limpo = limpo.replace(",", "")
    elif "," in limpo:
        partes = limpo.split(",")
        if len(partes[-1]) == 2:
            limpo = limpo.replace(",", ".")
        else:
            limpo = limpo.replace(",", "")
    try:
        return float(limpo)
    except ValueError:
        return None


def _seguir_redirect_html(html: str) -> Optional[str]:
    """AliExpress às vezes envia uma interstitial com redirect via meta refresh
    ou window.location.href em JS. Tenta extrair a próxima URL."""
    m = re.search(
        r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*content=["\'][^"\']*url=([^"\'>\s]+)',
        html, re.I,
    )
    if m:
        return m.group(1)
    m = re.search(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)', html)
    if m:
        return m.group(1)
    return None


def _buscar_pagina(url: str) -> requests.Response:
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    # Segue redirect via meta-refresh / window.location até 3 vezes
    for _ in range(3):
        if len(resp.text) > 30_000:
            break
        nova = _seguir_redirect_html(resp.text)
        if not nova:
            break
        if nova.startswith("//"):
            nova = "https:" + nova
        elif nova.startswith("/"):
            nova = "https://www.aliexpress.com" + nova
        try:
            resp = session.get(nova, timeout=30, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException:
            break
    return resp


def _runparams(html: str) -> Optional[dict]:
    """Tenta extrair window.runParams (estrutura interna do PDP do AliExpress).
    Retorna o dict 'data' se conseguir."""
    m = re.search(r"window\.runParams\s*=\s*(\{[\s\S]*?\});\s*</script>", html)
    if not m:
        m = re.search(r"window\.runParams\s*=\s*(\{[\s\S]*?\})\s*;", html)
    if not m:
        return None
    bruto = m.group(1)
    try:
        obj = json.loads(bruto)
    except json.JSONDecodeError:
        return None
    return obj.get("data") if isinstance(obj, dict) else None


def _ler_meta(soup: BeautifulSoup, prop: str) -> Optional[str]:
    el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if el and el.get("content"):
        return el["content"].strip()
    return None


def _profile_em_uso() -> bool:
    """Verifica se há algum processo Chrome rodando usando o profile do
    scraper. Se sim, o Playwright trava ao tentar abrir o mesmo profile."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={_PROFILE_DIR}"],
            capture_output=True, text=True, timeout=3,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _extrair_via_playwright(url: str) -> Optional[dict]:
    """Caminho principal: usa Chrome do sistema com profile persistente
    (anti-bot bypass) pra carregar a SPA do AliExpress, espera os preços
    hidratarem e extrai do DOM já renderizado.
    Retorna dict com os campos preenchidos ou None se falhar/timeout."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return None

    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    # Seletores de preço atual — cobre classes com hash variável
    _SELS_ATUAL = ", ".join([
        '[class*="price-default--current--"]',
        '[class*="price--current--"]',
        '[class*="price--currentPriceText--"]',
        '[class*="Price--current"]',
        '[class*="pdp-price-current"]',
        '[class*="pdpPrice_current"]',
    ])

    _PROFILE_DIR.mkdir(exist_ok=True)

    # Se o Chrome do desbloqueio ainda estiver aberto, o profile está locked
    # e o Playwright trava por minutos. Detecta isso antes e propaga sinal
    # de captcha pra UI poder mostrar instrução clara.
    if _profile_em_uso():
        return {"_captcha": True, "_motivo": "profile_em_uso"}

    dados = None
    try:
        with sync_playwright() as p:
            # launch_persistent_context: mantém cookies entre execuções pra evitar
            # cair no challenge anti-bot toda vez. Usa headless=False com janela
            # off-screen (-2400,-2400) — a janela existe mas fica fora da área
            # visível. headless=False é menos detectável que headless=True.
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR),
                channel="chrome",
                headless=False,
                user_agent=UA,
                locale="pt-BR",
                viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=-2400,-2400",
                    "--window-size=1280,900",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            try:
                ctx.add_init_script(_STEALTH_INIT)
                # Bloqueia só imagens/vídeos — mantém CSS e fontes
                ctx.route("**/*", lambda r: (
                    r.abort() if r.request.resource_type in {"image", "media"}
                    else r.continue_()
                ))
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=25000)

                # Se caiu no challenge anti-bot (URL contém /punish ou tmd),
                # faz warmup na home pra setar cookies de sessão e tenta de novo.
                if "_____tmd_____" in page.url or "/punish" in page.url:
                    try:
                        page.goto("https://pt.aliexpress.com/",
                                  wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3500)
                        page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    except PWTimeout:
                        pass

                # Detecta captcha slider (NC verify) — precisa de interação humana
                conteudo = page.content()
                tem_captcha = (
                    "/punish" in page.url
                    or "_____tmd_____" in page.url
                    or ("slide" in conteudo.lower() and "verify" in conteudo.lower()
                        and "nc-container" in conteudo.lower())
                    or "punish?x5secdata" in conteudo
                )
                if tem_captcha:
                    return {"_captcha": True}

                # Espera o preço aparecer no DOM; se timeout, aguarda rede estabilizar
                try:
                    page.wait_for_selector(_SELS_ATUAL, timeout=20000)
                except PWTimeout:
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PWTimeout:
                        pass  # prossegue e tenta extrair o que há no DOM

                dados = page.evaluate(
                    """() => {
                      // ---- helpers ----
                      const pick = sel => {
                        try {
                          const el = document.querySelector(sel);
                          return el ? (el.innerText || el.textContent || '').trim() : null;
                        } catch(e) { return null; }
                      };
                      const pickMany = (...sels) => {
                        for (const s of sels) { const v = pick(s); if (v) return v; }
                        return null;
                      };

                      // ---- título ----
                      let titulo = pickMany(
                        '[class*="title--wrap--"] h1',
                        '[class*="title--wrap--"]',
                        '[class*="product-title"]',
                        'h1[class*="title"]'
                      );
                      if (!titulo) {
                        const og = document.querySelector('meta[property="og:title"]');
                        if (og) titulo = og.content;
                      }

                      // ---- preço atual ----
                      let atual = pickMany(
                        '[class*="price-default--current--"]',
                        '[class*="price--currentPriceText--"]',
                        '[class*="price--current--"]',
                        '[class*="Price--current"]',
                        '[class*="pdpPrice_current"]',
                        '[class*="pdp-price-current"]'
                      );
                      // Fallback: varre todos os [class*=price] e pega o primeiro
                      // com R$/US$ e texto curto (provavelmente o preço principal)
                      if (!atual) {
                        const pat = /R\\$\\s*[\\d.,]+|US\\$\\s*[\\d.,]+/;
                        for (const el of document.querySelectorAll('[class*="price"],[class*="Price"]')) {
                          const t = (el.innerText || '').trim();
                          if (pat.test(t) && t.length < 25) { atual = t; break; }
                        }
                      }

                      // ---- preço original (riscado) ----
                      let original = pickMany(
                        '[class*="price-default--original--"]',
                        '[class*="price--originalText--"]',
                        '[class*="price--original--"]',
                        '[class*="Price--original"]',
                        '[class*="pdpPrice_original"]',
                        '[class*="pdp-price-original"]',
                        'del [class*="price"]',
                        's [class*="price"]'
                      );
                      if (!original) {
                        const pat = /R\\$\\s*[\\d.,]+|US\\$\\s*[\\d.,]+/;
                        for (const tag of ['del','s']) {
                          for (const el of document.querySelectorAll(tag)) {
                            const t = (el.innerText || '').trim();
                            if (pat.test(t) && t.length < 25) { original = t; break; }
                          }
                          if (original) break;
                        }
                      }

                      // ---- cupom ----
                      let cupom = null;
                      const candidatos = document.querySelectorAll(
                        '[class*="coupon-block--wrap--"],[class*="coupon-block--couponItem--"],[class*="coupon--"],[class*="Coupon--"],[class*="coupon_"]'
                      );
                      for (const el of candidatos) {
                        const t = (el.innerText || '').trim();
                        if (t && /OFF|cupom|R\\$|US\\s?\\$/i.test(t) && t.length < 120) {
                          cupom = t.split(/\\n+/).map(s=>s.trim()).filter(Boolean)[0];
                          break;
                        }
                      }

                      // ---- frete grátis ----
                      // AliExpress mostra "Frete grátis" no bloco de envio.
                      // Buscamos em qualquer elemento de shipping/logistic + fallback no body.
                      let freteGratis = false;
                      const shippingEls = document.querySelectorAll(
                        '[class*="shipping"],[class*="Shipping"],[class*="logistic"],[class*="Logistic"],[class*="dynamic-shipping"]'
                      );
                      for (const el of shippingEls) {
                        const t = (el.innerText || '').toLowerCase();
                        if (/frete\\s+gr[áa]tis|free\\s+shipping|env[íi]o\\s+gr[áa]tis/.test(t)) {
                          freteGratis = true;
                          break;
                        }
                      }
                      // Fallback: procura no body inteiro mas só primeiros 5000 chars
                      // (pra evitar pegar reviews/comentários onde alguém escreveu)
                      if (!freteGratis) {
                        const t = (document.body.innerText || '').slice(0, 5000).toLowerCase();
                        if (/frete\\s+gr[áa]tis|free\\s+shipping/.test(t)) {
                          freteGratis = true;
                        }
                      }

                      return { titulo, atual, original, cupom, freteGratis };
                    }"""
                )
            finally:
                ctx.close()
    except Exception:
        return None

    if not dados:
        return None

    # Sinal de captcha vindo do Playwright — propaga pra cima
    if dados.get("_captcha"):
        return {"_captcha": True}

    saida = {}
    if dados.get("titulo"):
        t = dados["titulo"]
        t = re.sub(r"\s*[-|]\s*AliExpress.*$", "", t, flags=re.I).strip()
        if t:
            saida["titulo"] = t
    if dados.get("atual"):
        saida["preco_atual"] = _parse_preco(dados["atual"])
    if dados.get("original"):
        p = _parse_preco(dados["original"])
        if p and saida.get("preco_atual") and p > saida["preco_atual"]:
            saida["preco_original"] = p
    if dados.get("cupom"):
        saida["cupom"] = dados["cupom"]
    if dados.get("freteGratis"):
        saida["frete_gratis"] = True
    return saida or None


def extrair_titulo_rapido(url: str) -> Optional[str]:
    """Tenta pegar SÓ o título via requests (og:title), sem Playwright e sem
    challenge. Best-effort: timeout curto, retorna None se não vier."""
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(url, timeout=10, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")
        og = _ler_meta(soup, "og:title")
        if og:
            t = re.sub(r"\s*[-|]\s*AliExpress.*$", "", og, flags=re.I).strip()
            return t or None
    except Exception:
        pass
    return None


def extrair_ali_manual(url: str) -> dict:
    """Modo manual do AliExpress: NÃO usa Playwright (que trava com captcha).
    Tenta só o título rápido via requests e devolve o resto vazio pro usuário
    preencher na mão. Instantâneo e nunca dá erro de bloqueio."""
    return {
        "plataforma": "aliexpress",
        "titulo": extrair_titulo_rapido(url),
        "descricao_curta": None,
        "preco_atual": None,
        "preco_original": None,
        "cupom": None,
        "frete_gratis": False,
        "url_original": url,
        "preencher_manual": True,
    }


def extrair_produto_ali(url: str) -> dict:
    resultado = {
        "plataforma": "aliexpress",
        "titulo": None,
        "descricao_curta": None,
        "preco_atual": None,
        "preco_original": None,
        "cupom": None,
        "frete_gratis": False,
        "url_original": url,
    }

    # 1) Caminho principal: Playwright + Chrome do sistema (resolve a SPA)
    via_pw = _extrair_via_playwright(url)
    if via_pw and via_pw.get("_captcha"):
        if via_pw.get("_motivo") == "profile_em_uso":
            resultado["erro"] = "chrome_aberto"
        else:
            resultado["erro"] = "captcha_aliexpress"
        return resultado
    # Sucesso real exige preço. No AliExpress, título sem preço = bloqueio
    # parcial (título vem da meta tag og:, mas o preço não hidrata porque o
    # anti-bot serviu uma versão incompleta) → trata como captcha.
    if via_pw and via_pw.get("preco_atual"):
        resultado.update(via_pw)
        return resultado
    if via_pw and via_pw.get("titulo") and not via_pw.get("preco_atual"):
        resultado["erro"] = "captcha_aliexpress"
        return resultado

    # 2) Fallback: requests + parsing estático (parcial, geralmente só título)
    try:
        resp = _buscar_pagina(url)
    except requests.RequestException as e:
        resultado["erro"] = f"Falha no request: {e}"
        return resultado

    soup = BeautifulSoup(resp.text, "lxml")

    # 1) Tenta window.runParams (mais estável e completo quando existe)
    runparams = _runparams(resp.text)
    if runparams:
        title_mod = runparams.get("titleModule") or {}
        if title_mod.get("subject"):
            resultado["titulo"] = title_mod["subject"]

        price_mod = runparams.get("priceModule") or {}
        # preço atual
        cur = price_mod.get("formatedActivityPrice") or price_mod.get("formatedPrice")
        if isinstance(cur, str):
            resultado["preco_atual"] = _parse_preco(cur)
        elif isinstance(price_mod.get("minActivityAmount"), dict):
            resultado["preco_atual"] = _parse_preco(
                price_mod["minActivityAmount"].get("formatedAmount")
            )
        elif isinstance(price_mod.get("minAmount"), dict):
            resultado["preco_atual"] = _parse_preco(
                price_mod["minAmount"].get("formatedAmount")
            )
        # preço original (riscado)
        orig = price_mod.get("formatedPrice")
        if isinstance(orig, str) and resultado["preco_atual"]:
            p = _parse_preco(orig)
            if p and p > resultado["preco_atual"]:
                resultado["preco_original"] = p

        cupom_mod = runparams.get("couponModule") or runparams.get("storeCouponModule") or {}
        if cupom_mod.get("couponList"):
            primeiro = cupom_mod["couponList"][0] if cupom_mod["couponList"] else None
            if isinstance(primeiro, dict):
                txt = primeiro.get("title") or primeiro.get("description")
                if txt:
                    resultado["cupom"] = txt

    # 2) Fallback: meta tags
    if not resultado["titulo"]:
        og_t = _ler_meta(soup, "og:title")
        if og_t:
            resultado["titulo"] = re.sub(r"\s*[-|]\s*AliExpress.*$", "", og_t, flags=re.I).strip()
        elif soup.title:
            resultado["titulo"] = re.sub(
                r"\s*[-|]\s*AliExpress.*$", "", soup.title.get_text(strip=True), flags=re.I
            ).strip() or None

    if resultado["preco_atual"] is None:
        og_p = _ler_meta(soup, "og:price:amount") or _ler_meta(soup, "product:price:amount")
        if og_p:
            resultado["preco_atual"] = _parse_preco(og_p)

    # 3) Fallback: DOM (PDP atual do AliExpress)
    if resultado["preco_atual"] is None:
        for sel in [
            "[class*='price--currentPriceText']",
            ".product-price-current .product-price-value",
            ".uniform-banner-box-price",
            "[data-pl='product-price'] [class*='price']",
            ".price-current",
        ]:
            el = soup.select_one(sel)
            if el:
                p = _parse_preco(el.get_text(" ", strip=True))
                if p:
                    resultado["preco_atual"] = p
                    break

    if resultado["preco_original"] is None:
        for sel in [
            "[class*='price--originalText']",
            "[class*='price--original'] [class*='price']",
            ".product-price-original .product-price-value",
            ".price-original",
            "del",
            "s",
        ]:
            for el in soup.select(sel):
                p = _parse_preco(el.get_text(" ", strip=True))
                if p and resultado["preco_atual"] and p > resultado["preco_atual"]:
                    resultado["preco_original"] = p
                    break
            if resultado["preco_original"]:
                break

    # 4) Cupom: fallback no texto bruto
    if not resultado["cupom"]:
        texto = soup.get_text(" ", strip=True)
        m = re.search(
            r"(?:coupon|cupom|cup[óo]n)[^.\n]{0,80}?(\d{1,3}\s?%|R\$\s?\d+(?:[.,]\d{2})?|US\s?\$?\s?\d+(?:[.,]\d{2})?)",
            texto, re.IGNORECASE,
        )
        if m:
            resultado["cupom"] = m.group(0).strip()

    # Se não conseguiu nem título nem preço, considera falha de extração
    # (página 404, bloqueio anti-bot, ou redirect que não chegou ao produto).
    if not resultado["titulo"] and resultado["preco_atual"] is None:
        resultado["erro"] = (
            "Não foi possível extrair os dados da página do AliExpress "
            "(possível bloqueio anti-bot ou link inválido)"
        )

    return resultado


if __name__ == "__main__":
    import sys
    url_teste = sys.argv[1] if len(sys.argv) > 1 else (
        "https://pt.aliexpress.com/item/1005005844456001.html"
    )
    dados = extrair_produto_ali(url_teste)
    print(json.dumps(dados, indent=2, ensure_ascii=False))
