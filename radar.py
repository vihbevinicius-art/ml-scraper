"""Radar de Ofertas — varre as ofertas do Mercado Livre no nicho de
foto/áudio/vídeo e devolve os produtos em promoção (com % de desconto).

Usa o Jina Reader (r.jina.ai) pra ler as páginas de oferta de um IP não
bloqueado — funciona tanto local quanto no Render. Não pega o preço em R$
(some na renderização do ML), mas pega título, link, % de desconto e frete,
que é o sinal do que vale a pena caçar. O preço o operador copia ao abrir.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

# Categorias de oferta do ML relevantes pro nicho de foto/vídeo/áudio.
# MLB1039 = Câmeras e Acessórios (coração do nicho: câmeras, lentes,
#           microfones, tripés, cartões, estabilizadores).
# MLB1000 = Eletrônicos, Áudio e Vídeo (fones, caixas, áudio pro — filtrado
#           depois pela marca/categoria pra não encher de TV genérica).
CATEGORIAS_OFERTAS = {
    "MLB1039": "Câmeras e Acessórios",
    "MLB1000": "Áudio e Vídeo",
}

_HEADING = re.compile(
    r"###\s*\[([^\]]+)\]\((https://[^\)]*?(?:/p/MLB\d+|-MLB\d+|/MLB-[^\)?#]+))[^\)]*\)"
)


def _limpar_link(url: str) -> str:
    """Remove tracking (?pdp_filters=…, #polycard…) — deixa o link limpo."""
    return re.split(r"[?#]", url, maxsplit=1)[0]


def _ler_ofertas_jina(url: str, timeout: int = 35) -> str:
    """Lê a página de ofertas via Jina Reader. Retorna o markdown ou ''."""
    try:
        r = requests.get(
            "https://r.jina.ai/" + url,
            headers={"x-timeout": str(timeout - 5), "Accept": "text/plain"},
            timeout=timeout,
        )
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _parse_ofertas(markdown: str, categoria_nome: str) -> list:
    """Extrai ofertas do markdown do Jina. Cada oferta: título, link,
    desconto (%), frete_gratis, oferta_do_dia."""
    ofertas = []
    vistos = set()
    matches = list(_HEADING.finditer(markdown))
    for i, m in enumerate(matches):
        titulo = m.group(1).strip()
        link = _limpar_link(m.group(2))
        if link in vistos:
            continue
        vistos.add(link)

        # Contexto ANTES do heading (onde ficam desconto/frete/selo),
        # limitado ao fim do heading anterior pra não vazar de outra oferta.
        ini = matches[i - 1].end() if i > 0 else max(0, m.start() - 400)
        bloco = markdown[ini:m.start()]

        desc_m = re.search(r"(\d{1,2})%\s*OFF", bloco)
        desconto = int(desc_m.group(1)) if desc_m else None
        frete = bool(re.search(r"(?:frete|chegar[áa])\s+gr[áa]tis", bloco, re.I))
        oferta_dia = "OFERTA DO DIA" in bloco.upper()

        ofertas.append({
            "titulo": titulo,
            "url": link,
            "desconto": desconto,
            "frete_gratis": frete,
            "oferta_do_dia": oferta_dia,
            "categoria_ml": categoria_nome,
        })
    return ofertas


def _buscar_categoria(item) -> list:
    cat_id, cat_nome = item
    url = f"https://www.mercadolivre.com.br/ofertas?category={cat_id}"
    return _parse_ofertas(_ler_ofertas_jina(url), cat_nome)


def buscar_ofertas() -> list:
    """Busca ofertas de todas as categorias configuradas (em paralelo pra não
    somar o tempo do Jina). Retorna lista unificada (sem duplicatas por link)."""
    todas = []
    vistos = set()
    with ThreadPoolExecutor(max_workers=len(CATEGORIAS_OFERTAS)) as ex:
        for ofertas in ex.map(_buscar_categoria, CATEGORIAS_OFERTAS.items()):
            for oferta in ofertas:
                if oferta["url"] not in vistos:
                    vistos.add(oferta["url"])
                    todas.append(oferta)
    return todas


if __name__ == "__main__":
    import json
    ofertas = buscar_ofertas()
    print(f"{len(ofertas)} ofertas encontradas:\n")
    print(json.dumps(ofertas[:10], indent=2, ensure_ascii=False))
