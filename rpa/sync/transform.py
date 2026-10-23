"""Transformações puras (sem I/O): normalização de tipos e malha viária estimada.

Tudo aqui é testável sem Postgres nem Neo4j.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rpa.sync.extract import DadosExtraidos

RAIO_TERRA_KM = 6371.0088

_CODIGOS_ZONA_SP: dict[str, int] = {
    'CENTRO':     0,
    'ZONA_SUL':   1,
    'ZONA_OESTE': 2,
    'ZONA_LESTE': 3,
    'ZONA_NORTE': 4,
}

_VELOCIDADE_BASE_ZONA_KMH: dict[str, float] = {
    'CENTRO':     16.5,
    'ZONA_SUL':   21.0,
    'ZONA_OESTE': 19.5,
    'ZONA_LESTE': 23.0,
    'ZONA_NORTE': 22.0,
}

_TORTUOSIDADE_ZONA_SP: dict[str, float] = {
    'CENTRO':     1.42,
    'ZONA_SUL':   1.36,
    'ZONA_OESTE': 1.38,
    'ZONA_LESTE': 1.34,
    'ZONA_NORTE': 1.40,
}

_FATOR_RETENCAO_PAR_ZONA: dict[tuple[str, str], float] = {
    ('CENTRO', 'CENTRO'):         1.35,
    ('ZONA_OESTE', 'ZONA_SUL'):   1.32,
    ('ZONA_SUL', 'ZONA_OESTE'):   1.32,
    ('ZONA_LESTE', 'CENTRO'):     1.40,
    ('CENTRO', 'ZONA_LESTE'):     1.38,
    ('ZONA_LESTE', 'ZONA_OESTE'): 1.48,
    ('ZONA_OESTE', 'ZONA_LESTE'): 1.48,
    ('ZONA_NORTE', 'ZONA_SUL'):   1.45,
    ('ZONA_SUL', 'ZONA_NORTE'):   1.45,
    ('ZONA_NORTE', 'CENTRO'):     1.30,
    ('CENTRO', 'ZONA_NORTE'):     1.30,
}


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
# Geometria e Zonas de SP
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(h))


def inferir_zona_sp(
    lat: float | None = None,
    lng: float | None = None,
    cep: str | None = None,
) -> str:
    """Infere a macrozona de São Paulo por prefixo de CEP ou quadrante geográfico."""
    if cep:
        digitos = ''.join(ch for ch in str(cep) if ch.isdigit())
        if len(digitos) >= 2:
            prefixo = int(digitos[:2])
            if prefixo == 1:
                return 'CENTRO'
            if prefixo == 2:
                return 'ZONA_NORTE'
            if prefixo in (3, 8):
                return 'ZONA_LESTE'
            if prefixo == 4:
                return 'ZONA_SUL'
            if prefixo == 5:
                return 'ZONA_OESTE'

    if lat is not None and lng is not None:
        if abs(lat - (-23.5505)) <= 0.022 and abs(lng - (-46.6333)) <= 0.022:
            return 'CENTRO'
        if lat > -23.525:
            return 'ZONA_NORTE'
        if lng > -46.590:
            return 'ZONA_LESTE'
        if lat < -23.585:
            return 'ZONA_SUL'
        return 'ZONA_OESTE'

    return 'CENTRO'


def fator_horario_pico_sp(hora: int) -> float:
    hora = int(hora) % 24
    if 7 <= hora <= 9:
        return 1.55
    if 17 <= hora <= 19:
        return 1.65
    if 10 <= hora <= 16:
        return 1.22
    if 20 <= hora <= 21:
        return 1.12
    return 0.90


def extrair_features_trecho_sp(
    lat_origem: float,
    lng_origem: float,
    zona_origem: str,
    lat_destino: float,
    lng_destino: float,
    zona_destino: str,
    hora_saida: int = 9,
    peso_estimado_kg: float = 200.0,
    fator_tortuosidade_padrao: float | None = None,
) -> tuple[list[float], float]:
    """Calcula as 9 features numéricas do trecho e o tempo estimado (em minutos)."""
    dist_hav = haversine_km(lat_origem, lng_origem, lat_destino, lng_destino)
    tort_o = _TORTUOSIDADE_ZONA_SP.get(zona_origem, 1.38)
    tort_d = _TORTUOSIDADE_ZONA_SP.get(zona_destino, 1.38)
    fator_tort = fator_tortuosidade_padrao if fator_tortuosidade_padrao is not None else (tort_o + tort_d) / 2.0
    if zona_origem != zona_destino and fator_tortuosidade_padrao is None:
        fator_tort += 0.06

    dist_viaria = round(dist_hav * fator_tort, 3)
    cod_o = float(_CODIGOS_ZONA_SP.get(zona_origem, 0))
    cod_d = float(_CODIGOS_ZONA_SP.get(zona_destino, 0))
    mudanca = 1.0 if zona_origem != zona_destino else 0.0
    retencao = _FATOR_RETENCAO_PAR_ZONA.get((zona_origem, zona_destino), 1.25 if mudanca else 1.05)
    fator_h = fator_horario_pico_sp(hora_saida)

    vel_o = _VELOCIDADE_BASE_ZONA_KMH.get(zona_origem, 20.0)
    vel_d = _VELOCIDADE_BASE_ZONA_KMH.get(zona_destino, 20.0)
    vel_efetiva = max(6.5, ((vel_o + vel_d) / 2.0) / (retencao * fator_h))

    tempo_percurso = (dist_viaria / vel_efetiva) * 60.0
    tempo_manobra = 4.0 + (peso_estimado_kg * 0.012) + (2.5 if zona_destino == 'CENTRO' else 0.0)
    tempo_total = round(max(2.0, tempo_percurso + tempo_manobra), 2)

    features = [
        round(dist_hav, 3),
        dist_viaria,
        cod_o,
        cod_d,
        mudanca,
        round(retencao, 3),
        round(fator_h, 3),
        float(hora_saida),
        round(float(peso_estimado_kg), 2),
    ]
    return features, tempo_total


def _otimizar_sequencia_paradas(
    coop: dict[str, Any],
    paradas: list[dict[str, Any]],
    ponto_por_id: dict[str, dict[str, Any]],
    hora_saida: int = 9,
) -> tuple[list[dict[str, Any]], float]:
    """Ordena as paradas minimizando o tempo total estimado (Exato <= 7 paradas, 2-Opt > 7)."""
    n = len(paradas)
    if n <= 1:
        return list(paradas), 0.0

    com_coord = []
    sem_coord = []
    for p in paradas:
        pt = ponto_por_id.get(p['ponto_id'], {})
        if pt.get('lat') is not None and pt.get('lng') is not None:
            com_coord.append(p)
        else:
            sem_coord.append(p)

    if coop.get('lat') is None or coop.get('lng') is None or len(com_coord) <= 1:
        return list(paradas), 0.0

    m = len(com_coord)
    nos: list[dict[str, Any]] = [
        {
            'lat': float(coop['lat']),
            'lng': float(coop['lng']),
            'zona': inferir_zona_sp(coop.get('lat'), coop.get('lng'), coop.get('cep')),
            'peso': 0.0,
        }
    ]
    for p in com_coord:
        pt = ponto_por_id[p['ponto_id']]
        nos.append(
            {
                'lat': float(pt['lat']),
                'lng': float(pt['lng']),
                'zona': inferir_zona_sp(pt.get('lat'), pt.get('lng'), pt.get('cep')),
                'peso': float(p.get('peso_estimado_kg') or 200.0),
            }
        )

    total_nos = m + 1
    matriz_tempo = [[0.0] * total_nos for _ in range(total_nos)]
    for i in range(total_nos):
        for j in range(1, total_nos):
            if i == j:
                continue
            _, t_min = extrair_features_trecho_sp(
                lat_origem=nos[i]['lat'],
                lng_origem=nos[i]['lng'],
                zona_origem=nos[i]['zona'],
                lat_destino=nos[j]['lat'],
                lng_destino=nos[j]['lng'],
                zona_destino=nos[j]['zona'],
                hora_saida=hora_saida,
                peso_estimado_kg=nos[j]['peso'],
            )
            matriz_tempo[i][j] = t_min

    def custo(seq: list[int]) -> float:
        total = 0.0
        atual = 0
        for idx in seq:
            total += matriz_tempo[atual][idx]
            atual = idx
        return total

    seq_original = list(range(1, total_nos))
    tempo_original = custo(seq_original)

    if m <= 7:
        melhor_seq = seq_original
        melhor_custo = tempo_original
        for perm in itertools.permutations(seq_original):
            c = custo(list(perm))
            if c < melhor_custo:
                melhor_custo = c
                melhor_seq = list(perm)
    else:
        nao_visitados = set(seq_original)
        atual = 0
        melhor_seq = []
        while nao_visitados:
            prox = min(nao_visitados, key=lambda idx: matriz_tempo[atual][idx])
            melhor_seq.append(prox)
            nao_visitados.remove(prox)
            atual = prox

        melhorou = True
        while melhorou:
            melhorou = False
            custo_atual = custo(melhor_seq)
            for i in range(m - 1):
                for k in range(i + 1, m):
                    cand = melhor_seq[:i] + list(reversed(melhor_seq[i : k + 1])) + melhor_seq[k + 1 :]
                    if custo(cand) + 1e-6 < custo_atual:
                        melhor_seq = cand
                        melhorou = True
                        break
                if melhorou:
                    break

    ordenadas = [com_coord[idx - 1] for idx in melhor_seq] + sem_coord
    return ordenadas, round(tempo_original, 2)


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
    otimizar_ordem: bool = True,
    hora_saida_padrao: int = 9,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Mapeia os nós :Rota e as relações :POSSUI_ROTA e :PARADA com distâncias e features de ML."""
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
        hora_saida = int(r.get("hora_saida") or hora_saida_padrao)
        paradas_iniciais = sorted(
            paradas_por_rota.get(rota_id, []),
            key=lambda x: int(x.get("ordem") or 1),
        )

        if otimizar_ordem and len(paradas_iniciais) > 1:
            paradas, tempo_original_min = _otimizar_sequencia_paradas(
                coop=coop,
                paradas=paradas_iniciais,
                ponto_por_id=ponto_por_id,
                hora_saida=hora_saida,
            )
        else:
            paradas = paradas_iniciais
            tempo_original_min = 0.0

        lat_ant, lng_ant = coop.get("lat"), coop.get("lng")
        zona_ant = inferir_zona_sp(lat_ant, lng_ant, coop.get("cep"))
        dist_acumulada = 0.0
        tempo_acumulado = 0.0

        for idx, parada in enumerate(paradas, start=1):
            ponto = ponto_por_id[parada["ponto_id"]]
            lat_p, lng_p = ponto.get("lat"), ponto.get("lng")
            zona_p = inferir_zona_sp(lat_p, lng_p, ponto.get("cep"))
            peso_kg = float(parada.get("peso_estimado_kg") or 200.0)

            trecho_km: float | None = None
            dist_hav_km: float | None = None
            tempo_trecho_min: float | None = None
            feats: list[float] | None = None

            if (
                lat_ant is not None
                and lng_ant is not None
                and lat_p is not None
                and lng_p is not None
            ):
                feats, tempo_trecho_min = extrair_features_trecho_sp(
                    lat_origem=float(lat_ant),
                    lng_origem=float(lng_ant),
                    zona_origem=zona_ant,
                    lat_destino=float(lat_p),
                    lng_destino=float(lng_p),
                    zona_destino=zona_p,
                    hora_saida=hora_saida,
                    peso_estimado_kg=peso_kg,
                    fator_tortuosidade_padrao=fator_tortuosidade,
                )
                dist_hav_km = feats[0]
                trecho_km = feats[1]
                dist_acumulada = round(dist_acumulada + trecho_km, 3)
                tempo_acumulado = round(tempo_acumulado + tempo_trecho_min, 2)

            if lat_p is not None and lng_p is not None:
                lat_ant, lng_ant = lat_p, lng_p
                zona_ant = zona_p

            rel_paradas.append(
                {
                    "rota_id": rota_id,
                    "ponto_id": parada["ponto_id"],
                    "ordem": idx,
                    "nome_local": parada.get("nome_local") or ponto.get("nome"),
                    "tipo_local": parada.get("tipo_local") or ponto.get("tipo_local"),
                    "zona_origem": zona_ant if feats is None else inferir_zona_sp(
                        coop.get("lat") if idx == 1 else lat_ant,
                        coop.get("lng") if idx == 1 else lng_ant,
                    ),
                    "zona_destino": zona_p,
                    "distancia_haversine_km": dist_hav_km,
                    "distancia_trecho_km": trecho_km,
                    "distancia_acumulada_km": dist_acumulada if trecho_km is not None else None,
                    "codigo_zona_origem": feats[2] if feats is not None else None,
                    "codigo_zona_destino": feats[3] if feats is not None else None,
                    "mudanca_zona": feats[4] if feats is not None else None,
                    "fator_retencao": feats[5] if feats is not None else None,
                    "fator_horario_sp": feats[6] if feats is not None else None,
                    "hora_saida": float(hora_saida),
                    "peso_estimado_kg": peso_kg,
                    "tempo_trecho_min": tempo_trecho_min,
                    "tempo_acumulado_min": tempo_acumulado if tempo_trecho_min is not None else None,
                }
            )

        if tempo_original_min <= 0.0:
            tempo_original_min = tempo_acumulado
        economia_min = round(max(0.0, tempo_original_min - tempo_acumulado), 2)
        economia_pct = (
            round((economia_min / tempo_original_min) * 100.0, 2)
            if tempo_original_min > 0
            else 0.0
        )

        nos_rotas.append(
            {
                "id": rota_id,
                "coop_id": coop_id,
                "nome": r.get("nome"),
                "esta_ativa": bool(r.get("esta_ativa", True)),
                "qtd_paradas": len(paradas),
                "distancia_total_km": round(dist_acumulada, 3),
                "tempo_total_estimado_min": round(tempo_acumulado, 2),
                "tempo_ordem_original_min": round(tempo_original_min, 2),
                "economia_estimada_min": economia_min,
                "economia_percentual": economia_pct,
                "hora_saida": hora_saida,
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
