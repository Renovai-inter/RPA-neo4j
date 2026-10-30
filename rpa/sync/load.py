"""Carga no Neo4j: upsert idempotente (UNWIND + MERGE) e limpeza do que sumiu do Postgres.

Estratégia "marca e varre":
  - toda execução gera um `sync_id`;
  - cada nó/relação gravado recebe `sync_id` da execução;
  - ao final (e só se tudo deu certo), apaga nós/relações GERENCIADOS com `sync_id` antigo,
    preservando registros criados/enriquecidos pela API de IA (`gerenciado_ia = true` ou `tempo_real_min IS NOT NULL`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from rpa.sync.transform import Payload

log = logging.getLogger(__name__)

ROTULOS_GERENCIADOS = ("Cooperativa", "EmpresaCompradora", "PontoDeColeta", "Material", "Rota")
RELACOES_GERENCIADAS = (
    "PROCESSA_MATERIAL",
    "COLETA_DE",
    "POSSUI_ROTA",
    "PARADA",
    "FORNECE_MATERIAL",
    "INTERESSADA_EM",
    "FAVORITOU",
    "SUBCATEGORIA_DE",
    "CONECTADA_A",
)

ESQUEMA = [
    "CREATE CONSTRAINT cooperativa_id IF NOT EXISTS FOR (n:Cooperativa) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT empresa_id IF NOT EXISTS FOR (n:EmpresaCompradora) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ponto_id IF NOT EXISTS FOR (n:PontoDeColeta) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT material_id IF NOT EXISTS FOR (n:Material) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT rota_id IF NOT EXISTS FOR (n:Rota) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT local_id IF NOT EXISTS FOR (n:Local) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT syncrun_id IF NOT EXISTS FOR (n:SyncRun) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX material_nome IF NOT EXISTS FOR (n:Material) ON (n.nome)",
    "CREATE INDEX local_geo_chave IF NOT EXISTS FOR (n:Local) ON (n.geo_chave)",
    "CREATE POINT INDEX local_location IF NOT EXISTS FOR (n:Local) ON (n.location)",
]

# Propriedades gravadas em cada tipo de nó (o resto do registro é ignorado)
_PROPS_ENDERECO = ("endereco_id", "cep", "cidade", "bairro", "geo_chave", "lat", "lng")
PROPS = {
    "Cooperativa": ("nome", "numero_cooperados", "score_conformidade", "qtd_avaliacoes", *_PROPS_ENDERECO),
    "EmpresaCompradora": ("nome", *_PROPS_ENDERECO),
    "PontoDeColeta": ("nome", "tipo_local", "logradouro", "numero", *_PROPS_ENDERECO),
    "Rota": (
        "nome",
        "coop_id",
        "esta_ativa",
        "qtd_paradas",
        "distancia_total_km",
        "tempo_total_estimado_min",
        "tempo_ordem_original_min",
        "economia_estimada_min",
        "economia_percentual",
        "hora_saida",
    ),
}

PROPS_PARADA = (
    "ordem",
    "ponto_id",
    "nome_local",
    "tipo_local",
    "zona_origem",
    "zona_destino",
    "distancia_haversine_km",
    "distancia_trecho_km",
    "distancia_acumulada_km",
    "codigo_zona_origem",
    "codigo_zona_destino",
    "mudanca_zona",
    "fator_retencao",
    "fator_horario_sp",
    "hora_saida",
    "peso_estimado_kg",
    "tempo_trecho_min",
    "tempo_acumulado_min",
)

Q_NO_LOCAL = """
UNWIND $linhas AS r
MERGE (n:{rotulo} {{id: r.id}})
SET n:Local,
    n += r.props,
    n.location = CASE WHEN r.props.lat IS NULL OR r.props.lng IS NULL THEN null
                      ELSE point({{latitude: r.props.lat, longitude: r.props.lng}}) END,
    n.sync_id = $sync_id
"""

Q_MATERIAL = """
UNWIND $linhas AS r
MERGE (m:Material {id: r.id})
SET m.nome = r.nome, m.sync_id = $sync_id
"""

Q_ROTA = """
UNWIND $linhas AS r
MERGE (rt:Rota {id: r.id})
SET rt += r.props, rt.sync_id = $sync_id
"""

Q_RELACAO = """
UNWIND $linhas AS r
MATCH (a:{rotulo_a} {{id: r.a}})
MATCH (b:{rotulo_b} {{id: r.b}})
MERGE (a)-[x:{tipo}]->(b)
SET x += r.props, x.sync_id = $sync_id
"""

# Só grava onde há medição: sem isso um null apagaria o tempo real já registrado na aresta.
Q_TEMPOS_REAIS = """
UNWIND $linhas AS r
MATCH (:Rota {id: r.rota_id})-[x:PARADA]->(:PontoDeColeta {id: r.ponto_id})
SET x.tempo_real_min = r.tempo_real_min,
    x.tempo_real_amostras = r.amostras
"""

Q_LIMPAR_NOS = """
MATCH (n:{rotulo})
WHERE (n.sync_id IS NULL OR n.sync_id <> $sync_id)
  AND coalesce(n.gerenciado_ia, false) = false
DETACH DELETE n
RETURN count(*) AS removidos
"""

Q_LIMPAR_RELACOES = """
MATCH ()-[x:{tipo}]->()
WHERE (x.sync_id IS NULL OR x.sync_id <> $sync_id)
  AND coalesce(x.gerenciado_ia, false) = false
  AND x.tempo_real_min IS NULL
DELETE x
RETURN count(*) AS removidos
"""

Q_CACHE_GEO = """
MATCH (n:Local)
WHERE n.geo_chave IS NOT NULL AND n.lat IS NOT NULL AND n.lng IS NOT NULL
RETURN n.geo_chave AS chave, n.lat AS lat, n.lng AS lng
"""

Q_REGISTRAR_EXECUCAO = """
MERGE (s:SyncRun {id: $sync_id})
SET s += $dados
WITH s
MATCH (antigo:SyncRun)
WITH antigo ORDER BY antigo.iniciado_em DESC
SKIP $manter
DELETE antigo
"""


def novo_sync_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _lotes(itens: list[Any], tamanho: int) -> Iterator[list[Any]]:
    for i in range(0, len(itens), tamanho):
        yield itens[i : i + tamanho]


def _props(registro: dict[str, Any], campos: Iterable[str]) -> dict[str, Any]:
    return {c: registro.get(c) for c in campos}


class CarregadorNeo4j:
    def __init__(self, uri: str, usuario: str, senha: str, database: str, tamanho_lote: int = 500):
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(usuario, senha))
        self._driver.verify_connectivity()
        self._database = database
        self._lote = tamanho_lote

    def fechar(self) -> None:
        self._driver.close()

    # -- utilitários --------------------------------------------------------

    def _escrever(self, query: str, linhas: list[dict[str, Any]], sync_id: str) -> int:
        total = 0
        with self._driver.session(database=self._database) as sessao:
            for lote in _lotes(linhas, self._lote):
                sessao.execute_write(
                    lambda tx, l=lote: tx.run(query, linhas=l, sync_id=sync_id).consume()
                )
                total += len(lote)
        return total

    def _executar(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self._driver.session(database=self._database) as sessao:
            return sessao.execute_write(lambda tx: tx.run(query, **params).data())

    # -- etapas -------------------------------------------------------------

    def criar_esquema(self) -> None:
        with self._driver.session(database=self._database) as sessao:
            for ddl in ESQUEMA:
                sessao.run(ddl).consume()

    def ler_cache_geo(self) -> dict[str, tuple[float, float]]:
        with self._driver.session(database=self._database) as sessao:
            linhas = sessao.execute_read(lambda tx: tx.run(Q_CACHE_GEO).data())
        return {l["chave"]: (l["lat"], l["lng"]) for l in linhas}

    def carregar(self, payload: Payload, sync_id: str) -> dict[str, int]:
        gravados: dict[str, int] = {}

        # Nós
        for rotulo, registros in (
            ("Cooperativa", payload.cooperativas),
            ("EmpresaCompradora", payload.empresas),
            ("PontoDeColeta", payload.pontos),
        ):
            linhas = [{"id": r["id"], "props": _props(r, PROPS[rotulo])} for r in registros]
            gravados[rotulo] = self._escrever(Q_NO_LOCAL.format(rotulo=rotulo), linhas, sync_id)

        gravados["Material"] = self._escrever(Q_MATERIAL, payload.materiais, sync_id)

        linhas_rotas = [{"id": r["id"], "props": _props(r, PROPS["Rota"])} for r in payload.rotas]
        gravados["Rota"] = self._escrever(Q_ROTA, linhas_rotas, sync_id)

        # Relações
        relacoes = [
            ("SUBCATEGORIA_DE", "Material", "Material",
             [{"a": m["id"], "b": m["pai_id"], "props": {}} for m in payload.materiais if m.get("pai_id")]),
            ("PROCESSA_MATERIAL", "Cooperativa", "Material",
             [{"a": r["coop_id"], "b": r["material_id"],
               "props": _props(r, ("capacidade_mensal_kg", "estoque_atual_kg",
                                   "preco_sugerido_medio", "ofertado"))}
              for r in payload.processa]),
            ("COLETA_DE", "Cooperativa", "PontoDeColeta",
             [{"a": r["coop_id"], "b": r["ponto_id"], "props": _props(r, ("qtd_rotas", "rotas"))}
              for r in payload.coleta_de]),
            ("POSSUI_ROTA", "Cooperativa", "Rota",
             [{"a": r["coop_id"], "b": r["rota_id"], "props": {}}
              for r in payload.possui_rota]),
            ("PARADA", "Rota", "PontoDeColeta",
             [{"a": r["rota_id"], "b": r["ponto_id"],
               "props": _props(r, PROPS_PARADA)}
              for r in payload.paradas]),
            ("FORNECE_MATERIAL", "PontoDeColeta", "Material",
             [{"a": r["ponto_id"], "b": r["material_id"],
               "props": {**_props(r, ("kg_estimado_periodo", "qtd_coletas")), "estimado": True}}
              for r in payload.fornece]),
            ("INTERESSADA_EM", "EmpresaCompradora", "Material",
             [{"a": r["empresa_id"], "b": r["material_id"], "props": {}} for r in payload.interesses]),
            ("FAVORITOU", "EmpresaCompradora", "Cooperativa",
             [{"a": r["empresa_id"], "b": r["coop_id"], "props": {"desde": r.get("data_criacao")}}
              for r in payload.favoritas]),
            ("CONECTADA_A", "Local", "Local",
             [{"a": c["a"], "b": c["b"],
               "props": {"distancia_km": c["distancia_km"], "tipo": c["tipo"], "estimada": True}}
              for c in payload.conexoes]),
        ]
        for tipo, rotulo_a, rotulo_b, linhas in relacoes:
            q = Q_RELACAO.format(rotulo_a=rotulo_a, rotulo_b=rotulo_b, tipo=tipo)
            gravados[tipo] = self._escrever(q, linhas, sync_id)

        # Depois das arestas :PARADA (o MATCH precisa delas)
        gravados["tempos_reais"] = self._escrever(Q_TEMPOS_REAIS, payload.tempos_reais, sync_id)

        log.info("gravado no Neo4j: %s", gravados)
        return gravados

    def limpar_obsoletos(self, sync_id: str) -> dict[str, int]:
        removidos: dict[str, int] = {}
        for tipo in RELACOES_GERENCIADAS:
            r = self._executar(Q_LIMPAR_RELACOES.format(tipo=tipo), sync_id=sync_id)
            removidos[tipo] = r[0]["removidos"] if r else 0
        for rotulo in ROTULOS_GERENCIADOS:
            r = self._executar(Q_LIMPAR_NOS.format(rotulo=rotulo), sync_id=sync_id)
            removidos[rotulo] = r[0]["removidos"] if r else 0
        log.info("removido do Neo4j (não existe mais no Postgres): %s", removidos)
        return removidos

    def registrar_execucao(self, sync_id: str, dados: dict[str, Any], manter: int = 30) -> None:
        self._executar(Q_REGISTRAR_EXECUCAO, sync_id=sync_id, dados=dados, manter=manter)
