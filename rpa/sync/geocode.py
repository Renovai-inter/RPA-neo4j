"""Geocodificação de endereços (CEP -> lat/lng) com cache.

O cache principal fica no próprio Neo4j: cada nó :Local guarda `geo_chave`, `lat` e `lng`.
Na execução seguinte, endereços cuja chave não mudou são reaproveitados sem chamar API.
Isso importa porque o Nominatim aceita no máximo ~1 requisição por segundo.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Callable

log = logging.getLogger(__name__)

Coord = tuple[float, float]
Provedor = Callable[[dict[str, Any]], Coord | None]


def _normalizar(txt: str | None) -> str:
    if not txt:
        return ""
    txt = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt).strip().lower()


def so_digitos(cep: str | None) -> str:
    return re.sub(r"\D", "", cep or "")


def chave_endereco(end: dict[str, Any]) -> str | None:
    """Chave estável do endereço; muda se CEP, logradouro, número ou cidade mudarem."""
    partes = [
        so_digitos(end.get("cep")),
        _normalizar(end.get("logradouro")),
        _normalizar(end.get("numero")),
        _normalizar(end.get("cidade")),
    ]
    if not any(partes):
        return None
    return "|".join(partes)


# ---------------------------------------------------------------------------
# Provedores
# ---------------------------------------------------------------------------

def provedor_brasilapi(sessao) -> Provedor:
    def buscar(end: dict[str, Any]) -> Coord | None:
        cep = so_digitos(end.get("cep"))
        if len(cep) != 8:
            return None
        resp = sessao.get(f"https://brasilapi.com.br/api/cep/v2/{cep}", timeout=10)
        if resp.status_code != 200:
            return None
        coords = ((resp.json().get("location") or {}).get("coordinates") or {})
        lat, lng = coords.get("latitude"), coords.get("longitude")
        if lat in (None, "") or lng in (None, ""):
            return None
        return float(lat), float(lng)

    return buscar


def _limpar_texto(txt: str | None) -> str:
    if not txt:
        return ""
    t = re.sub(r"(?i)\b(s/n|sem n[uú]mero|sn)\b", "", str(txt))
    return re.sub(r"\s+", " ", t).strip(", -")


def provedor_nominatim(sessao, user_agent: str, intervalo_s: float) -> Provedor:
    ultimo = [0.0]

    def _consultar(params: dict[str, str]) -> Coord | None:
        espera = intervalo_s - (time.monotonic() - ultimo[0])
        if espera > 0:
            time.sleep(espera)
        ultimo[0] = time.monotonic()
        resp = sessao.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": "1", "countrycodes": "br", **params},
            headers={"User-Agent": user_agent},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        dados = resp.json()
        if not dados:
            return None
        return float(dados[0]["lat"]), float(dados[0]["lon"])

    def buscar(end: dict[str, Any]) -> Coord | None:
        logr = _limpar_texto(end.get("logradouro"))
        num = _limpar_texto(end.get("numero"))
        bairro = _limpar_texto(end.get("bairro"))
        cidade = _limpar_texto(end.get("cidade"))

        # Extrair cidade se estiver embutida em bairro (ex: 'Centro, Osasco' -> bairro='Centro', cidade='Osasco')
        if not cidade and bairro and "," in bairro:
            partes_b = [p.strip() for p in bairro.split(",") if p.strip()]
            if len(partes_b) >= 2:
                cidade = partes_b[-1]
                bairro = ", ".join(partes_b[:-1])

        # 1) Tentar busca estruturada se tiver logradouro e cidade
        if logr and cidade:
            rua = f"{logr}, {num}".strip(", ") if num else logr
            achou = _consultar({"street": rua, "city": cidade})
            if achou:
                return achou
            if num:
                achou = _consultar({"street": logr, "city": cidade})
                if achou:
                    return achou

        # 2) Tentar busca por CEP
        cep = so_digitos(end.get("cep"))
        if len(cep) == 8:
            achou = _consultar({"postalcode": f"{cep[:5]}-{cep[5:]}"})
            if achou:
                return achou

        # 3) Fallback: busca livre (q) com partes de endereço disponíveis
        partes_q = [p for p in (logr, num, bairro, cidade) if p]
        if not partes_q and end.get("nome"):
            partes_q = [_limpar_texto(end["nome"])]

        if partes_q:
            termo = ", ".join(partes_q)
            achou = _consultar({"q": termo})
            if achou:
                return achou

        return None

    return buscar


def provedor_mock() -> Provedor:
    """Gera coordenadas determinísticas na Região Metropolitana de SP para dados de teste/faker.

    Ativado apenas se 'mock' for explicitamente incluído na lista de GEOCODERS (ex: GEOCODERS=brasilapi,nominatim,mock).
    Permite que o grafo fique conexo e recomendável em ambientes de teste com dados sintéticos.
    """
    import hashlib

    def buscar(end: dict[str, Any]) -> Coord | None:
        chave = chave_endereco(end) or str(end.get("id"))
        h = int(hashlib.md5(chave.encode()).hexdigest()[:8], 16)
        offset_lat = ((h % 1000) / 1000.0) * 0.30 - 0.15
        offset_lng = (((h // 1000) % 1000) / 1000.0) * 0.45 - 0.225
        return round(-23.5505 + offset_lat, 6), round(-46.6333 + offset_lng, 6)

    return buscar


def criar_provedores(nomes: tuple[str, ...], user_agent: str, intervalo_s: float) -> list[Provedor]:
    if not nomes:
        return []
    import requests

    sessao = requests.Session()
    disponiveis = {
        "brasilapi": lambda: provedor_brasilapi(sessao),
        "nominatim": lambda: provedor_nominatim(sessao, user_agent, intervalo_s),
        "mock": lambda: provedor_mock(),
    }
    provedores = []
    for nome in nomes:
        if nome not in disponiveis:
            raise ValueError(f"Geocoder desconhecido: {nome} (use brasilapi, nominatim, mock ou none)")
        provedores.append(disponiveis[nome]())
    return provedores


# ---------------------------------------------------------------------------
# Resolução
# ---------------------------------------------------------------------------

def resolver_coordenadas(
    registros: list[dict[str, Any]],
    cache: dict[str, Coord],
    provedores: list[Provedor],
) -> dict[str, int]:
    """Preenche `lat`, `lng` e `geo_chave` em cada registro (in-place).

    Prioridade: coordenada que já veio do Postgres > cache > provedores externos.
    Devolve estatísticas para o log.
    """
    stats = {"do_postgres": 0, "do_cache": 0, "geocodificados": 0, "sem_coordenada": 0}

    for reg in registros:
        reg["geo_chave"] = chave_endereco(reg)

        if reg.get("lat") is not None and reg.get("lng") is not None:
            stats["do_postgres"] += 1
            continue

        chave = reg["geo_chave"]
        if chave and chave in cache:
            reg["lat"], reg["lng"] = cache[chave]
            stats["do_cache"] += 1
            continue

        coord = None
        if chave:
            for provedor in provedores:
                try:
                    coord = provedor(reg)
                except Exception as exc:
                    log.warning("falha ao geocodificar %s: %s", chave, exc)
                    coord = None
                if coord:
                    break

        if coord:
            reg["lat"], reg["lng"] = coord
            cache[chave] = coord
            stats["geocodificados"] += 1
        else:
            reg["lat"] = reg["lng"] = None
            stats["sem_coordenada"] += 1
            log.warning("sem coordenada: id=%s chave=%s", reg.get("id"), chave)

    return stats
