"""Extração do Postgres do Renovaí (fonte da verdade).

Todas as queries usam apenas tabelas do schema oficial. As colunas
`enderecos.latitude` / `enderecos.longitude` ainda NÃO existem no schema; se forem
criadas no futuro, o job passa a usá-las automaticamente e só geocodifica o que faltar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Fonte(Protocol):
    """Qualquer coisa que execute SQL e devolva linhas como dicts."""

    def buscar(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


class FontePostgres:
    """Implementação real com psycopg 3."""

    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        # Leitura apenas: protege contra qualquer escrita acidental no banco de produção.
        self._conn.execute("SET default_transaction_read_only = on")

    def buscar(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params or {})
            return list(cur.fetchall())

    def fechar(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_TEM_COORDENADAS = """
SELECT COUNT(*) = 2 AS tem
FROM information_schema.columns
WHERE table_name = 'enderecos' AND column_name IN ('latitude', 'longitude')
"""

# {coords} é trocado por "e.latitude::float AS lat, e.longitude::float AS lng" ou por NULLs.
_COLS_ENDERECO = """
    e.endereco_id::text AS endereco_id,
    e.cep, e.logradouro, e.numero, e.bairro, e.cidade,
    {coords}
"""

SQL_COOPERATIVAS = """
SELECT
    c.cooperativa_id::text AS id,
    c.nome,
    c.numero_cooperados,
    {endereco},
    av.media_nota / 5.0 AS score_conformidade,
    av.qtd_avaliacoes
FROM cooperativas c
LEFT JOIN perfis p     ON p.cooperativa_id = c.cooperativa_id
LEFT JOIN enderecos e  ON e.endereco_id = p.endereco_id
LEFT JOIN LATERAL (
    -- avaliacoes.avaliado_id pode guardar o perfil_id ou o cooperativa_id;
    -- aceitamos os dois até o contrato da API Spring ser confirmado.
    SELECT AVG(a.nota)::float AS media_nota, COUNT(a.nota) AS qtd_avaliacoes
    FROM avaliacoes a
    WHERE a.avaliado_id IN (p.perfil_id, c.cooperativa_id)
) av ON TRUE
WHERE COALESCE(p.esta_ativo, TRUE)
"""

SQL_EMPRESAS = """
SELECT
    em.empresa_id::text AS id,
    em.nome,
    {endereco}
FROM empresas em
LEFT JOIN perfis p     ON p.empresa_id = em.empresa_id
LEFT JOIN enderecos e  ON e.endereco_id = p.endereco_id
WHERE COALESCE(p.esta_ativo, TRUE)
"""

# Cada endereço que aparece em alguma rota ativa vira um PontoDeColeta.
SQL_PONTOS_COLETA = """
SELECT
    e.endereco_id::text AS id,
    MIN(re.nome_local) AS nome,
    MIN(re.tipo_local) AS tipo_local,
    {endereco}
FROM rotas_enderecos re
JOIN rotas r      ON r.rota_id = re.rota_id AND r.esta_ativa
JOIN enderecos e  ON e.endereco_id = re.endereco_id
GROUP BY e.endereco_id
"""

# O nó Material do grafo é a CATEGORIA (materiais não tem nome; é a oferta de uma cooperativa).
SQL_MATERIAIS = """
SELECT
    categoria_id::text     AS id,
    nome_categoria         AS nome,
    categoria_pai_id::text AS pai_id
FROM categorias_materiais
"""

# Capacidade = média mensal de ENTRADAS de estoque na janela; também traz estoque atual e preço.
SQL_PROCESSA_MATERIAL = """
WITH entradas AS (
    SELECT es.cooperativa_id, m.categoria_id, SUM(me.quantidade_kg) AS kg
    FROM movimentacoes_estoques me
    JOIN estoques es ON es.estoque_id = me.estoque_id
    JOIN materiais m ON m.material_id = es.material_id
    WHERE me.quantidade_kg > 0
      AND me.data_movimentacao >= NOW() - make_interval(months => %(meses)s::int)
    GROUP BY 1, 2
),
estoque AS (
    SELECT es.cooperativa_id, m.categoria_id, SUM(es.quantidade_kg) AS kg
    FROM estoques es
    JOIN materiais m ON m.material_id = es.material_id
    GROUP BY 1, 2
),
oferta AS (
    SELECT cooperativa_id, categoria_id, AVG(preco_sugerido) AS preco
    FROM materiais
    WHERE cooperativa_id IS NOT NULL AND esta_disponivel
    GROUP BY 1, 2
),
chaves AS (
    SELECT cooperativa_id, categoria_id FROM entradas
    UNION SELECT cooperativa_id, categoria_id FROM estoque
    UNION SELECT cooperativa_id, categoria_id FROM oferta
)
SELECT
    k.cooperativa_id::text                         AS coop_id,
    k.categoria_id::text                           AS material_id,
    (COALESCE(en.kg, 0) / %(meses)s::int)::float        AS capacidade_mensal_kg,
    COALESCE(st.kg, 0)::float                      AS estoque_atual_kg,
    o.preco::float                                 AS preco_sugerido_medio,
    (o.cooperativa_id IS NOT NULL)                 AS ofertado
FROM chaves k
JOIN cooperativas c ON c.cooperativa_id = k.cooperativa_id
LEFT JOIN entradas en ON en.cooperativa_id = k.cooperativa_id AND en.categoria_id = k.categoria_id
LEFT JOIN estoque  st ON st.cooperativa_id = k.cooperativa_id AND st.categoria_id = k.categoria_id
LEFT JOIN oferta   o  ON o.cooperativa_id  = k.cooperativa_id AND o.categoria_id  = k.categoria_id
"""

SQL_COLETA_DE = """
SELECT
    r.cooperativa_id::text      AS coop_id,
    re.endereco_id::text        AS ponto_id,
    COUNT(DISTINCT r.rota_id)   AS qtd_rotas,
    ARRAY_AGG(DISTINCT r.nome)  AS rotas
FROM rotas r
JOIN rotas_enderecos re ON re.rota_id = r.rota_id
WHERE r.esta_ativa
GROUP BY 1, 2
"""

# A coleta aponta para a ROTA, não para o endereço. O kg triado é rateado
# igualmente entre os pontos da rota -> valor ESTIMADO.
SQL_FORNECE_MATERIAL = """
WITH pontos_por_rota AS (
    SELECT rota_id, COUNT(*) AS n FROM rotas_enderecos GROUP BY rota_id
)
SELECT
    re.endereco_id::text                       AS ponto_id,
    m.categoria_id::text                       AS material_id,
    SUM(t.quantidade_kg / ppr.n)::float        AS kg_estimado_periodo,
    COUNT(DISTINCT co.evento_id)               AS qtd_coletas
FROM triagens t
JOIN coletas co           ON co.evento_id = t.coleta_id
JOIN rotas_enderecos re   ON re.rota_id = co.rota_id
JOIN pontos_por_rota ppr  ON ppr.rota_id = co.rota_id
JOIN materiais m          ON m.material_id = t.material_id
WHERE t.data_evento >= NOW() - make_interval(days => %(dias)s::int)
GROUP BY 1, 2
"""

SQL_INTERESSES = """
SELECT empresa_id::text AS empresa_id, categoria_id::text AS material_id
FROM empresa_materiais_interesses
"""

SQL_FAVORITAS = """
SELECT empresa_id::text AS empresa_id, cooperativa_id::text AS coop_id, data_criacao
FROM empresa_cooperativas_favoritas
"""

SQL_ROTAS = """
SELECT
    r.rota_id::text        AS id,
    r.cooperativa_id::text AS coop_id,
    r.nome                 AS nome,
    r.esta_ativa           AS esta_ativa
FROM rotas r
WHERE r.esta_ativa
"""

SQL_ROTAS_PARADAS = """
SELECT
    re.rota_id::text       AS rota_id,
    r.cooperativa_id::text AS coop_id,
    re.endereco_id::text   AS ponto_id,
    re.nome_local          AS nome_local,
    re.tipo_local          AS tipo_local,
    re.ordem               AS ordem
FROM rotas_enderecos re
JOIN rotas r ON r.rota_id = re.rota_id
WHERE r.esta_ativa
ORDER BY re.rota_id, re.ordem ASC
"""


# Viagens que o motorista fez (gravadas pela API-Redis ao finalizar). As colunas endereco_id e
# data_conclusao só existem depois da versão da API-Redis que registra a hora de cada coleta.
SQL_TEM_TEMPOS_EXECUCAO = """
SELECT COUNT(*) = 2 AS tem
FROM information_schema.columns
WHERE table_name = 'rota_execucoes_paradas' AND column_name IN ('endereco_id', 'data_conclusao')
"""

SQL_TEMPOS_EXECUCAO = """
SELECT
    e.execucao_id::text  AS execucao_id,
    e.rota_id::text      AS rota_id,
    e.data_inicio        AS data_inicio,
    p.endereco_id::text  AS ponto_id,
    p.data_conclusao     AS data_conclusao
FROM rota_execucoes e
JOIN rota_execucoes_paradas p ON p.execucao_id = e.execucao_id
WHERE p.status = 'CONCLUIDA'
  AND p.endereco_id IS NOT NULL
  AND p.data_conclusao IS NOT NULL
  AND e.data_inicio IS NOT NULL
  AND e.data_finalizacao >= NOW() - make_interval(days => %(dias)s::int)
ORDER BY e.execucao_id, p.data_conclusao
"""


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

@dataclass
class DadosExtraidos:
    cooperativas: list[dict[str, Any]] = field(default_factory=list)
    empresas: list[dict[str, Any]] = field(default_factory=list)
    pontos: list[dict[str, Any]] = field(default_factory=list)
    materiais: list[dict[str, Any]] = field(default_factory=list)
    rotas: list[dict[str, Any]] = field(default_factory=list)
    rotas_paradas: list[dict[str, Any]] = field(default_factory=list)
    processa: list[dict[str, Any]] = field(default_factory=list)
    coleta_de: list[dict[str, Any]] = field(default_factory=list)
    fornece: list[dict[str, Any]] = field(default_factory=list)
    interesses: list[dict[str, Any]] = field(default_factory=list)
    favoritas: list[dict[str, Any]] = field(default_factory=list)
    execucoes: list[dict[str, Any]] = field(default_factory=list)

    def resumo(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.__dict__.items()}


def montar_sql(fonte: Fonte) -> dict[str, str]:
    """Monta as queries que dependem de o schema ter (ou não) lat/lng em enderecos."""
    tem_coords = bool(fonte.buscar(SQL_TEM_COORDENADAS)[0]["tem"])
    coords = (
        "e.latitude::float AS lat, e.longitude::float AS lng"
        if tem_coords
        else "NULL::float AS lat, NULL::float AS lng"
    )
    log.info("enderecos com latitude/longitude no schema: %s", tem_coords)
    endereco = _COLS_ENDERECO.format(coords=coords).strip()
    return {
        "cooperativas": SQL_COOPERATIVAS.format(endereco=endereco),
        "empresas": SQL_EMPRESAS.format(endereco=endereco),
        "pontos": SQL_PONTOS_COLETA.format(endereco=endereco),
    }


def extrair(fonte: Fonte, capacidade_janela_meses: int, fornece_janela_dias: int) -> DadosExtraidos:
    sql = montar_sql(fonte)
    dados = DadosExtraidos(
        cooperativas=fonte.buscar(sql["cooperativas"]),
        empresas=fonte.buscar(sql["empresas"]),
        pontos=fonte.buscar(sql["pontos"]),
        materiais=fonte.buscar(SQL_MATERIAIS),
        rotas=fonte.buscar(SQL_ROTAS),
        rotas_paradas=fonte.buscar(SQL_ROTAS_PARADAS),
        processa=fonte.buscar(SQL_PROCESSA_MATERIAL, {"meses": capacidade_janela_meses}),
        coleta_de=fonte.buscar(SQL_COLETA_DE),
        fornece=fonte.buscar(SQL_FORNECE_MATERIAL, {"dias": fornece_janela_dias}),
        interesses=fonte.buscar(SQL_INTERESSES),
        favoritas=fonte.buscar(SQL_FAVORITAS),
        execucoes=(
            fonte.buscar(SQL_TEMPOS_EXECUCAO, {"dias": fornece_janela_dias})
            if fonte.buscar(SQL_TEM_TEMPOS_EXECUCAO)[0]["tem"]
            else []
        ),
    )
    log.info("extraído do Postgres: %s", dados.resumo())
    return dados
