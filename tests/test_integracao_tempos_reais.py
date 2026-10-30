"""Integração: Postgres (viagens gravadas pela API-Redis) -> sync -> Neo4j -> consulta de treino da IA.

Só roda com bancos descartáveis (o teste APAGA o Neo4j e recria as tabelas no Postgres):
    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=teste -e POSTGRES_DB=renovai_teste postgres:16-alpine
    docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/senha-teste-123 neo4j:5-community
    PG_DSN_TESTE=postgresql://postgres:teste@localhost:55432/renovai_teste \
    NEO4J_URI_TESTE=bolt://localhost:7687 NEO4J_PASSWORD_TESTE=senha-teste-123 pytest tests
"""
import os
import unittest
import uuid
from unittest.mock import patch

PG_DSN = os.getenv("PG_DSN_TESTE")
NEO4J_URI = os.getenv("NEO4J_URI_TESTE")
NEO4J_SENHA = os.getenv("NEO4J_PASSWORD_TESTE")

# Só as colunas que as consultas do RPA leem (o schema real é o da Spring API).
SCHEMA = """
DROP TABLE IF EXISTS cooperativas, perfis, enderecos, avaliacoes, empresas, rotas, rotas_enderecos,
    categorias_materiais, materiais, estoques, movimentacoes_estoques, triagens, coletas,
    empresa_materiais_interesses, empresa_cooperativas_favoritas, rota_execucoes_paradas,
    rota_execucoes CASCADE;
CREATE TABLE cooperativas (cooperativa_id UUID PRIMARY KEY, nome TEXT, numero_cooperados INT);
CREATE TABLE enderecos (endereco_id UUID PRIMARY KEY, cep TEXT, logradouro TEXT, numero TEXT,
    bairro TEXT, cidade TEXT, latitude NUMERIC, longitude NUMERIC);
CREATE TABLE perfis (perfil_id UUID PRIMARY KEY, cooperativa_id UUID, empresa_id UUID,
    endereco_id UUID, esta_ativo BOOLEAN);
CREATE TABLE avaliacoes (avaliado_id UUID, nota NUMERIC);
CREATE TABLE empresas (empresa_id UUID PRIMARY KEY, nome TEXT);
CREATE TABLE rotas (rota_id UUID PRIMARY KEY, cooperativa_id UUID, nome TEXT, esta_ativa BOOLEAN);
CREATE TABLE rotas_enderecos (rota_id UUID, endereco_id UUID, nome_local TEXT, tipo_local TEXT, ordem INT);
CREATE TABLE categorias_materiais (categoria_id UUID PRIMARY KEY, nome_categoria TEXT, categoria_pai_id UUID);
CREATE TABLE materiais (material_id UUID PRIMARY KEY, categoria_id UUID, cooperativa_id UUID,
    preco_sugerido NUMERIC, esta_disponivel BOOLEAN);
CREATE TABLE estoques (estoque_id UUID PRIMARY KEY, cooperativa_id UUID, material_id UUID, quantidade_kg NUMERIC);
CREATE TABLE movimentacoes_estoques (estoque_id UUID, quantidade_kg NUMERIC, data_movimentacao TIMESTAMP);
CREATE TABLE coletas (evento_id UUID PRIMARY KEY, rota_id UUID);
CREATE TABLE triagens (coleta_id UUID, material_id UUID, quantidade_kg NUMERIC, data_evento TIMESTAMP);
CREATE TABLE empresa_materiais_interesses (empresa_id UUID, categoria_id UUID);
CREATE TABLE empresa_cooperativas_favoritas (empresa_id UUID, cooperativa_id UUID, data_criacao TIMESTAMP);
CREATE TABLE rota_execucoes (execucao_id UUID PRIMARY KEY, rota_id UUID, motorista_id UUID,
    status_final TEXT, data_inicio TIMESTAMP, data_finalizacao TIMESTAMP);
CREATE TABLE rota_execucoes_paradas (parada_execucao_id UUID PRIMARY KEY, execucao_id UUID,
    status TEXT, ordem INT, endereco_id UUID, data_conclusao TIMESTAMP);
"""

# Cópia da QUERY_BUSCAR_TRECHOS_TREINAMENTO do repo IA (app/repository/neo4j/rota_repository.py):
# é o que o POST /rotas/retreinar lê do grafo. Se a IA mudar a consulta, atualize aqui.
QUERY_TREINO_IA = """
    MATCH (:Rota)-[rel:PARADA]->(:PontoDeColeta)
    WHERE rel.distancia_haversine_km IS NOT NULL
      AND rel.distancia_trecho_km IS NOT NULL
      AND rel.codigo_zona_origem IS NOT NULL
      AND rel.codigo_zona_destino IS NOT NULL
      AND rel.mudanca_zona IS NOT NULL
      AND rel.fator_retencao IS NOT NULL
      AND rel.fator_horario_sp IS NOT NULL
      AND rel.hora_saida IS NOT NULL
      AND rel.peso_estimado_kg IS NOT NULL
      AND (
          ($apenas_reais = true AND rel.tempo_real_min IS NOT NULL)
          OR ($apenas_reais = false AND coalesce(rel.tempo_real_min, rel.tempo_trecho_min) IS NOT NULL)
      )
    RETURN coalesce(rel.tempo_real_min, rel.tempo_trecho_min) AS tempo_total_min
"""


@unittest.skipUnless(PG_DSN and NEO4J_URI and NEO4J_SENHA, "defina PG_DSN_TESTE, NEO4J_URI_TESTE e NEO4J_PASSWORD_TESTE")
class TemposReaisIntegracaoTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from neo4j import GraphDatabase

        self.neo4j = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_SENHA))
        with self.neo4j.session() as s:
            s.run("MATCH (n) DETACH DELETE n").consume()

        self.coop, self.rota = str(uuid.uuid4()), str(uuid.uuid4())
        # Sé (cooperativa) e três pontos em linha para o leste: a ordem otimizada é a mesma da rota
        locais = {
            "coop": (-23.5505, -46.6333, "Sé"),
            "A": (-23.5614, -46.5994, "Mooca"),
            "B": (-23.5401, -46.5762, "Tatuapé"),
            "C": (-23.5236, -46.5427, "Penha"),
        }
        self.end = {k: str(uuid.uuid4()) for k in locais}
        with psycopg.connect(PG_DSN, autocommit=True) as pg:
            pg.execute(SCHEMA)
            for k, (lat, lng, bairro) in locais.items():
                pg.execute("INSERT INTO enderecos VALUES (%s, '03000-000', 'Rua Teste', '1', %s, 'São Paulo', %s, %s)",
                           (self.end[k], bairro, lat, lng))
            pg.execute("INSERT INTO cooperativas VALUES (%s, 'Coop Teste', 10)", (self.coop,))
            pg.execute("INSERT INTO perfis VALUES (%s, %s, NULL, %s, true)", (str(uuid.uuid4()), self.coop, self.end["coop"]))
            pg.execute("INSERT INTO rotas VALUES (%s, %s, 'Rota Leste', true)", (self.rota, self.coop))
            for i, k in enumerate("ABC", start=1):
                pg.execute("INSERT INTO rotas_enderecos VALUES (%s, %s, %s, 'COLETA', %s)",
                           (self.rota, self.end[k], f"Ponto {k}", i))
            # duas viagens: A em 12/18 min, B +20/+22 min, C +9/+11 min; a 2ª pulou nada
            for minutos in ((12, 32, 41), (18, 40, 51)):
                ex = str(uuid.uuid4())
                pg.execute("INSERT INTO rota_execucoes VALUES (%s, %s, %s, 'FINALIZADA', "
                           "NOW() - interval '2 hours', NOW() - interval '1 hour')", (ex, self.rota, str(uuid.uuid4())))
                for i, (k, m) in enumerate(zip("ABC", minutos)):
                    pg.execute("INSERT INTO rota_execucoes_paradas VALUES (%s, %s, 'CONCLUIDA', %s, %s, "
                               "NOW() - interval '2 hours' + make_interval(mins => %s))",
                               (str(uuid.uuid4()), ex, i, self.end[k], m))

    def tearDown(self):
        self.neo4j.close()

    def test_sync_grava_tempo_real_que_a_ia_usa_no_treino(self):
        from rpa.sync.main import run

        ambiente = {"PG_DSN": PG_DSN, "NEO4J_URI": NEO4J_URI, "NEO4J_USER": "neo4j",
                    "NEO4J_PASSWORD": NEO4J_SENHA, "GEOCODERS": "none"}
        with patch.dict(os.environ, ambiente), patch("rpa.sync.config._carregar_dotenv"):
            resultado = run(sem_geocode=True, retreinar_ia=False)

        self.assertEqual(resultado["gravado"]["tempos_reais"], 3)
        with self.neo4j.session() as s:
            arestas = s.run(
                "MATCH (:Rota {id: $rota})-[x:PARADA]->(p:PontoDeColeta) "
                "RETURN p.id AS ponto, x.ordem AS ordem, x.tempo_real_min AS t, x.tempo_real_amostras AS n "
                "ORDER BY x.ordem", rota=self.rota).data()
            treino = s.run(QUERY_TREINO_IA, apenas_reais=True).data()
        self.assertEqual([a["ponto"] for a in arestas], [self.end[k] for k in "ABC"])
        self.assertEqual([(a["t"], a["n"]) for a in arestas], [(15.0, 2), (21.0, 2), (10.0, 2)])
        self.assertEqual(sorted(t["tempo_total_min"] for t in treino), [10.0, 15.0, 21.0])

        # um novo sync sem viagens novas não apaga a medição (nem a aresta)
        with patch.dict(os.environ, ambiente), patch("rpa.sync.config._carregar_dotenv"), \
             patch("rpa.sync.extract.SQL_TEMPOS_EXECUCAO", "SELECT 1 WHERE false"):
            run(sem_geocode=True, retreinar_ia=False)
        with self.neo4j.session() as s:
            self.assertEqual(len(s.run(QUERY_TREINO_IA, apenas_reais=True).data()), 3)


if __name__ == "__main__":
    unittest.main()
