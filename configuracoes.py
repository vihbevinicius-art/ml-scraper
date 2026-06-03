import json
import os
import threading

ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configuracoes.json")
_lock = threading.Lock()

CABECALHOS_PADRAO = [
    "🔥 PRODUTOS ESPECIAIS DO DIA HOJE NO MERCADO LIVRE 🔥",
    "📸 ACHADO DO DIA PRA QUEM AMA FOTOGRAFIA 📸",
    "⚡ PREÇO BAIXOU NO ML — CORRE! ⚡",
    "🎯 OFERTA IMPERDÍVEL DE HOJE NO MERCADO LIVRE 🎯",
    "💥 TÁ BARATO! OLHA ESSE PRODUTO 💥",
]


def _ler() -> dict:
    if not os.path.exists(ARQUIVO):
        return {"cabecalhos": CABECALHOS_PADRAO}
    try:
        with open(ARQUIVO, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"cabecalhos": CABECALHOS_PADRAO}


def _escrever(config: dict) -> None:
    with open(ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def obter_cabecalhos() -> list:
    with _lock:
        config = _ler()
    return config.get("cabecalhos") or CABECALHOS_PADRAO[:]


def salvar_cabecalhos(cabecalhos: list) -> list:
    limpos = [c.strip() for c in cabecalhos if c.strip()]
    if not limpos:
        limpos = CABECALHOS_PADRAO[:]
    with _lock:
        config = _ler()
        config["cabecalhos"] = limpos
        _escrever(config)
    return limpos
