"""Transformações puras (sem I/O): normalização de tipos e malha viária estimada.

Tudo aqui é testável sem Postgres nem Neo4j.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rpa.sync.extract import DadosExtraidos

RAIO_TERRA_KM = 6371.0088


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

def _limpar(valor: Any) -> Any:
    """Converte tipos do Postgres em tipos aceitos pelo driver do Neo4j."""
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, (list, tuple)):
        return [_limpar(v) for v in valor]
    return valor


def limpar_linhas(linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _limpar(v) for k, v in linha.items()} for linha in linhas]


# ---------------------------------------------------------------------------
# Geometria
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(h))


@dataclass
class _UniaoBusca:
    pai: dict[str, str] = field(default_factory=dict)

    def achar(self, x: str) -> str:
        self.pai.setdefault(x, x)
        while self.pai[x] != x:
            self.pai[x] = self.pai[self.pai[x]]
            x = self.pai[x]
        return x

    def unir(self, a: str, b: str) -> bool:
        ra, rb = self.achar(a), self.achar(b)
        if ra == rb:
            return False
        self.pai[ra] = rb
        return True


def construir_malha(
    locais: list[dict[str, Any]],
    k: int,
    fator_tortuosidade: float,
    max_aresta_km: float,
) -> list[dict[str, Any]]:
    """Gera as arestas CONECTADA_A entre locais com coordenada.

    1. k vizinhos mais próximos de cada local (até `max_aresta_km`).
    2. Pontes: enquanto houver componentes desconexos, liga cada par de componentes
       pelo par de locais mais próximo (ignora `max_aresta_km`), para que toda empresa
       alcance alguma cooperativa na travessia.

    distancia_km = linha reta (haversine) x fator de tortuosidade -> é uma ESTIMATIVA.
    Complexidade O(n²): suficiente para alguns milhares de locais.
    """
    pts = [l for l in locais if l.get("lat") is not None and l.get("lng") is not None]
    n = len(pts)
    if n < 2:
        return []

    def dist(i: int, j: int) -> float:
        a, b = pts[i], pts[j]
        return haversine_km(a["lat"], a["lng"], b["lat"], b["lng"]) * fator_tortuosidade

    matriz = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            matriz[i][j] = matriz[j][i] = dist(i, j)

    arestas: dict[tuple[str, str], dict[str, Any]] = {}
    uniao = _UniaoBusca()
    for p in pts:
        uniao.achar(p["id"])

    def adicionar(i: int, j: int, tipo: str) -> None:
        a, b = sorted((pts[i]["id"], pts[j]["id"]))
        if (a, b) not in arestas:
            arestas[(a, b)] = {"a": a, "b": b, "distancia_km": round(matriz[i][j], 3), "tipo": tipo}
        uniao.unir(a, b)

    # 1) k vizinhos mais próximos
    for i in range(n):
        vizinhos = sorted((matriz[i][j], j) for j in range(n) if j != i)
        for d, j in vizinhos[:k]:
            if d <= max_aresta_km:
                adicionar(i, j, "knn")

    # 2) pontes entre componentes (estilo Kruskal sobre os componentes)
    while True:
        componentes: dict[str, list[int]] = {}
        for i, p in enumerate(pts):
            componentes.setdefault(uniao.achar(p["id"]), []).append(i)
        if len(componentes) <= 1:
            break
        melhor: tuple[float, int, int] | None = None
        grupos = list(componentes.values())
        base = grupos[0]
        for outro in grupos[1:]:
            for i in base:
                for j in outro:
                    if melhor is None or matriz[i][j] < melhor[0]:
                        melhor = (matriz[i][j], i, j)
        assert melhor is not None
        adicionar(melhor[1], melhor[2], "ponte")

    return sorted(arestas.values(), key=lambda e: (e["a"], e["b"]))


# ---------------------------------------------------------------------------
# Rotas e Recomendação Multicritério
# ---------------------------------------------------------------------------

def construir_rotas(
    rotas: list[dict[str, Any]],
    rotas_paradas: list[dict[str, Any]],
    cooperativas: list[dict[str, Any]],
    pontos: list[dict[str, Any]],
    fator_tortuosidade: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Mapeia os nós :Rota e as relações :POSSUI_ROTA e :PARADA com distâncias estimadas."""
    coop_por_id = {c["id"]: c for c in cooperativas}
    ponto_por_id = {p["id"]: p for p in pontos}

    paradas_por_rota: dict[str, list[dict[str, Any]]] = {}
    for p in limpar_linhas(rotas_paradas):
        if p.get("ponto_id") in ponto_por_id:
            paradas_por_rota.setdefault(p["rota_id"], []).append(p)

    nos_rotas: list[dict[str, Any]] = []
    possui_rota: list[dict[str, Any]] = []
    rel_paradas: list[dict[str, Any]] = []

    for r in limpar_linhas(rotas):
        coop_id = r.get("coop_id")
        if coop_id not in coop_por_id:
            continue

        rota_id = r["id"]
        coop = coop_por_id[coop_id]
        paradas = sorted(paradas_por_rota.get(rota_id, []), key=lambda x: int(x.get("ordem") or 1))

        lat_ant, lng_ant = coop.get("lat"), coop.get("lng")
        dist_acumulada = 0.0

        for idx, parada in enumerate(paradas, start=1):
            ponto = ponto_por_id[parada["ponto_id"]]
            lat_p, lng_p = ponto.get("lat"), ponto.get("lng")
            trecho_km: float | None = None
            if (
                lat_ant is not None
                and lng_ant is not None
                and lat_p is not None
                and lng_p is not None
            ):
                trecho_km = round(
                    haversine_km(lat_ant, lng_ant, lat_p, lng_p) * fator_tortuosidade, 3
                )
                dist_acumulada = round(dist_acumulada + trecho_km, 3)

            if lat_p is not None and lng_p is not None:
                lat_ant, lng_ant = lat_p, lng_p

            rel_paradas.append(
                {
                    "rota_id": rota_id,
                    "ponto_id": parada["ponto_id"],
                    "ordem": int(parada.get("ordem") or idx),
                    "nome_local": parada.get("nome_local"),
                    "tipo_local": parada.get("tipo_local"),
                    "distancia_trecho_km": trecho_km,
                    "distancia_acumulada_km": dist_acumulada if trecho_km is not None else None,
                }
            )

        nos_rotas.append(
            {
                "id": rota_id,
                "coop_id": coop_id,
                "nome": r.get("nome"),
                "esta_ativa": bool(r.get("esta_ativa", True)),
                "qtd_paradas": len(paradas),
                "distancia_total_km": round(dist_acumulada, 3),
            }
        )
        possui_rota.append({"coop_id": coop_id, "rota_id": rota_id})

    return nos_rotas, possui_rota, rel_paradas


def calcular_score_recomendacao(
    *,
    estoque_atual_kg: float,
    capacidade_mensal_kg: float,
    quantidade_almejada: float,
    distancia_total_km: float,
    raio_max_km: float,
    score_conformidade: float | None = None,
    fontes_validadas: int = 0,
    favorita: bool = False,
) -> float:
    """Calcula o score ponderado (0–100) espelhando a fórmula de recomendar_cooperativas.cypher.

    Pesos:
      - Disponibilidade imediata em estoque na quantidade solicitada: 35%
      - Distância na malha até a cooperativa:                         30%
      - Capacidade produtiva mensal:                                  15%
      - Score de conformidade (avaliações):                           10%
      - Fontes de coleta validadas (5%) + Bônus favorita (5%):        10%
    """
    conformidade = 0.5 if score_conformidade is None else max(0.0, min(1.0, float(score_conformidade)))

    if quantidade_almejada <= 0:
        razao_estoque = 1.0
        razao_cap = 1.0
    else:
        razao_estoque = max(0.0, min(1.0, estoque_atual_kg / quantidade_almejada))
        razao_cap = max(0.0, min(1.0, capacidade_mensal_kg / (quantidade_almejada * 1.2)))

    if raio_max_km <= 0:
        razao_distancia = 1.0
    else:
        razao_distancia = max(0.0, min(1.0, 1.0 - (distancia_total_km / raio_max_km)))

    razao_fontes = 1.0 if fontes_validadas >= 2 else max(0.0, fontes_validadas / 2.0)
    bonus_favorita = 5.0 if favorita else 0.0

    score = (
        razao_estoque * 35.0
        + razao_distancia * 30.0
        + razao_cap * 15.0
        + conformidade * 10.0
        + razao_fontes * 5.0
        + bonus_favorita
    )
    return round(min(100.0, max(0.0, score)), 1)


# ---------------------------------------------------------------------------
# Payload final para o Neo4j
# ---------------------------------------------------------------------------

@dataclass
class Payload:
    cooperativas: list[dict[str, Any]]
    empresas: list[dict[str, Any]]
    pontos: list[dict[str, Any]]
    materiais: list[dict[str, Any]]
    processa: list[dict[str, Any]]
    coleta_de: list[dict[str, Any]]
    fornece: list[dict[str, Any]]
    interesses: list[dict[str, Any]]
    favoritas: list[dict[str, Any]]
    conexoes: list[dict[str, Any]]
    rotas: list[dict[str, Any]] = field(default_factory=list)
    possui_rota: list[dict[str, Any]] = field(default_factory=list)
    paradas: list[dict[str, Any]] = field(default_factory=list)

    def resumo(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.__dict__.items()}


def montar_payload(
    dados: DadosExtraidos, k: int, fator_tortuosidade: float, max_aresta_km: float
) -> Payload:
    cooperativas = limpar_linhas(dados.cooperativas)
    empresas = limpar_linhas(dados.empresas)
    pontos = limpar_linhas(dados.pontos)

    ids_coop = {c["id"] for c in cooperativas}
    ids_emp = {e["id"] for e in empresas}
    ids_ponto = {p["id"] for p in pontos}
    ids_mat = {m["id"] for m in dados.materiais}

    # Relações só entre nós que existem (evita MATCH vazio silencioso no Neo4j)
    processa = [r for r in limpar_linhas(dados.processa)
                if r["coop_id"] in ids_coop and r["material_id"] in ids_mat]
    coleta_de = [r for r in limpar_linhas(dados.coleta_de)
                 if r["coop_id"] in ids_coop and r["ponto_id"] in ids_ponto]
    fornece = [r for r in limpar_linhas(dados.fornece)
               if r["ponto_id"] in ids_ponto and r["material_id"] in ids_mat]
    interesses = [r for r in limpar_linhas(dados.interesses)
                  if r["empresa_id"] in ids_emp and r["material_id"] in ids_mat]
    favoritas = [r for r in limpar_linhas(dados.favoritas)
                 if r["empresa_id"] in ids_emp and r["coop_id"] in ids_coop]

    conexoes = construir_malha(cooperativas + empresas + pontos, k, fator_tortuosidade, max_aresta_km)
    rotas, possui_rota, paradas = construir_rotas(
        dados.rotas, dados.rotas_paradas, cooperativas, pontos, fator_tortuosidade
    )

    return Payload(
        cooperativas=cooperativas,
        empresas=empresas,
        pontos=pontos,
        materiais=limpar_linhas(dados.materiais),
        processa=processa,
        coleta_de=coleta_de,
        fornece=fornece,
        interesses=interesses,
        favoritas=favoritas,
        conexoes=conexoes,
        rotas=rotas,
        possui_rota=possui_rota,
        paradas=paradas,
    )

