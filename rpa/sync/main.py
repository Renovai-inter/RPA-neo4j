"""Orquestra o sync Postgres -> Neo4j.

Uso local:
    python -m sync.main              # sync completo
    python -m sync.main --dry-run    # só extrai e calcula; não escreve no Neo4j
    python -m sync.main --sem-geocode
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from rpa.sync.config import Config
from rpa.sync.extract import FontePostgres, extrair
from rpa.sync.geocode import criar_provedores, resolver_coordenadas
from rpa.sync.transform import montar_payload

log = logging.getLogger("renovai.graph_sync")


def run(dry_run: bool = False, sem_geocode: bool = False, permitir_vazio: bool = False) -> dict[str, Any]:
    from .load import CarregadorNeo4j, novo_sync_id

    inicio = time.monotonic()
    cfg = Config.from_env()
    sync_id = novo_sync_id()
    iniciado_em = datetime.now(timezone.utc).isoformat()
    log.info("sync %s iniciado (dry_run=%s)", sync_id, dry_run)

    # 1) Extração
    fonte = FontePostgres(cfg.pg_dsn)
    try:
        dados = extrair(fonte, cfg.capacidade_janela_meses, cfg.fornece_janela_dias)
    finally:
        fonte.fechar()

    if not dados.cooperativas and not permitir_vazio:
        # Proteção: um Postgres vazio (ou DSN errado) apagaria o grafo inteiro na limpeza.
        raise RuntimeError("Nenhuma cooperativa extraída; abortando. Use --permitir-vazio se for intencional.")

    carregador = None
    if not dry_run:
        cfg.exigir_neo4j()
        carregador = CarregadorNeo4j(
            cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password, cfg.neo4j_database, cfg.tamanho_lote
        )

    try:
        # 2) Coordenadas (cache no Neo4j + provedores externos)
        cache = carregador.ler_cache_geo() if carregador else {}
        provedores = [] if sem_geocode else criar_provedores(
            cfg.geocoders, cfg.nominatim_user_agent, cfg.nominatim_intervalo_s
        )
        geo = resolver_coordenadas(dados.cooperativas + dados.empresas + dados.pontos, cache, provedores)
        log.info("coordenadas: %s", geo)

        # 3) Transformação
        payload = montar_payload(dados, cfg.knn_k, cfg.fator_tortuosidade, cfg.max_aresta_km)
        log.info("payload: %s", payload.resumo())

        resultado: dict[str, Any] = {
            "sync_id": sync_id,
            "dry_run": dry_run,
            "extraido": dados.resumo(),
            "coordenadas": geo,
            "payload": payload.resumo(),
        }

        # 4) Carga + limpeza + registro
        if carregador:
            carregador.criar_esquema()
            resultado["gravado"] = carregador.carregar(payload, sync_id)
            resultado["removido"] = carregador.limpar_obsoletos(sync_id)

        resultado["duracao_s"] = round(time.monotonic() - inicio, 2)

        if carregador:
            # Propriedades do Neo4j precisam ser planas -> achata os contadores
            plano: dict[str, Any] = {"iniciado_em": iniciado_em, "duracao_s": resultado["duracao_s"]}
            for grupo in ("gravado", "removido", "coordenadas"):
                for chave, valor in resultado[grupo].items():
                    plano[f"{grupo}_{chave}"] = valor
            carregador.registrar_execucao(sync_id, plano)
    finally:
        if carregador:
            carregador.fechar()

    log.info("sync %s concluído em %ss", sync_id, resultado["duracao_s"])
    return resultado


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza o Postgres do Renovaí com o grafo Neo4j.")
    parser.add_argument("--dry-run", action="store_true", help="não escreve no Neo4j")
    parser.add_argument("--sem-geocode", action="store_true", help="não chama APIs de geocodificação")
    parser.add_argument("--permitir-vazio", action="store_true", help="permite sync sem cooperativas")
    parser.add_argument("--log", default="INFO", help="nível de log (DEBUG, INFO, WARNING)")
    args = parser.parse_args()

    logging.basicConfig(level=args.log.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    resultado = run(dry_run=args.dry_run, sem_geocode=args.sem_geocode, permitir_vazio=args.permitir_vazio)
    print(json.dumps(resultado, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
