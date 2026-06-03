import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historico.json")
_lock = threading.Lock()


def _ler() -> list:
    if not os.path.exists(ARQUIVO):
        return []
    try:
        with open(ARQUIVO, "r", encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _escrever(entradas: list) -> None:
    with open(ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(entradas, f, ensure_ascii=False, indent=2)


def registrar(
    url: str,
    titulo: Optional[str],
    preco: Optional[float],
    plataforma: Optional[str] = None,
) -> dict:
    entrada = {
        "url": url,
        "titulo": titulo,
        "preco": preco,
        "plataforma": plataforma,
        "copiado_em": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        entradas = _ler()
        entradas.insert(0, entrada)
        _escrever(entradas)
    return entrada


def listar(limite: int = 30) -> list:
    with _lock:
        return _ler()[:limite]


def limpar() -> None:
    with _lock:
        _escrever([])


def buscar_recentes(urls: list, dias: int = 7) -> dict:
    """Para cada URL recebida, devolve a entrada mais recente do histórico
    se for dos últimos `dias` dias. Mapeia url -> entrada."""
    alvo = set(urls)
    limite_ts = datetime.now(timezone.utc).timestamp() - dias * 86400
    achados: dict = {}
    with _lock:
        entradas = _ler()
    for e in entradas:
        u = e.get("url")
        if u not in alvo or u in achados:
            continue
        try:
            ts = datetime.fromisoformat(e["copiado_em"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if ts >= limite_ts:
            achados[u] = e
    return achados
