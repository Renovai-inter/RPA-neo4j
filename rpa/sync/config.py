"""Configuração do job, lida de variáveis de ambiente"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _carregar_dotenv() -> None:
    """Carrega o .env da raiz do projeto"""
    try:
        from dotenv import load_dotenv
    except ImportError:  
        return
    raiz = Path(__file__).resolve().parent.parent.parent
    load_dotenv(raiz / ".env", override=False)


def _env(nome: str, padrao: str | None = None, obrigatoria: bool = False) -> str | None:
    valor = os.getenv(nome, padrao)
    if obrigatoria and not valor:
        raise RuntimeError(f"Variável de ambiente obrigatória não definida: {nome}")
    return valor


@dataclass(frozen=True)
class Config:
    pg_dsn: str

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str

    geocoders: tuple[str, ...]
    nominatim_user_agent: str
    nominatim_intervalo_s: float

    knn_k: int
    fator_tortuosidade: float
    max_aresta_km: float

    capacidade_janela_meses: int
    fornece_janela_dias: int

    tamanho_lote: int

    ia_api_url: str = "http://localhost:8000"
    retreinar_rotas_apos_sync: bool = True

    def exigir_neo4j(self) -> None:
        if not self.neo4j_uri or not self.neo4j_password:
            raise RuntimeError("Defina NEO4J_URI e NEO4J_PASSWORD (ou rode com --dry-run).")
        if self.neo4j_uri.lower().startswith(("http://", "https://")):
            raise ValueError(
                f"NEO4J_URI inválida ({self.neo4j_uri}). O driver Neo4j requer o protocolo "
                "bolt://, neo4j:// ou neo4j+s:// (ex: bolt://localhost:7687)."
            )

    @staticmethod
    def from_env() -> "Config":
        _carregar_dotenv()

        dsn = _env("PG_DSN")
        if not dsn:
            host = _env("PG_HOST", obrigatoria=True)
            porta = _env("PG_PORT", "5432")
            banco = _env("PG_DATABASE", obrigatoria=True)
            usuario = _env("PG_USER", obrigatoria=True)
            senha = _env("PG_PASSWORD", obrigatoria=True)
            sslmode = _env("PG_SSLMODE", "prefer")
            dsn = (
                f"host={host} port={porta} dbname={banco} user={usuario} "
                f"password={senha} sslmode={sslmode}"
            )

        geocoders = tuple(
            g.strip().lower()
            for g in (_env("GEOCODERS", "brasilapi,nominatim") or "").split(",")
            if g.strip() and g.strip().lower() != "none"
        )

        return Config(
            pg_dsn=dsn,
            neo4j_uri=_env("NEO4J_URI", ""),
            neo4j_user=_env("NEO4J_USER", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD", ""),
            neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
            geocoders=geocoders,
            nominatim_user_agent=_env(
                "NOMINATIM_USER_AGENT", "renovai-graph-sync (defina um e-mail de contato)"
            ),
            nominatim_intervalo_s=float(_env("NOMINATIM_INTERVALO_S", "1.1")),
            knn_k=int(_env("KNN_K", "4")),
            fator_tortuosidade=float(_env("FATOR_TORTUOSIDADE", "1.3")),
            max_aresta_km=float(_env("MAX_ARESTA_KM", "50")),
            capacidade_janela_meses=int(_env("CAPACIDADE_JANELA_MESES", "3")),
            fornece_janela_dias=int(_env("FORNECE_JANELA_DIAS", "90")),
            tamanho_lote=int(_env("TAMANHO_LOTE", "500")),
            ia_api_url=_env("IA_API_URL", "http://localhost:8000"),
            retreinar_rotas_apos_sync=(_env("RETREINAR_ROTAS_APOS_SYNC", "true") or "true").strip().lower()
            in ("true", "1", "yes", "s", "sim"),
        )
