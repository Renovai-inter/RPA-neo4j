# renovai-graph-sync

Job que sincroniza o **Postgres do Renovaí** (fonte da verdade) com o **grafo Neo4j** usado pela
API de IA na recomendação de cooperativas e no planejamento de rotas.

```
Postgres (somente leitura) ──► extract ──► geocode ──► transform ──► load ──► Neo4j
                                 SQL       CEP→lat/lng   k vizinhos     MERGE
                                           (cache no     capacidade     + limpeza
                                            Neo4j)       score
```

A API de IA **só lê** o grafo. Este serviço é o **único que escreve** nele.

---

## Contrato do grafo

É isso que a API de IA pode assumir. Mudou aqui → atualize as queries da IA.

### Nós

| Label | Origem no Postgres | Propriedades |
|---|---|---|
| `:Cooperativa:Local` | `cooperativas` + `perfis` + `enderecos` | `id` (cooperativa_id), `nome`, `numero_cooperados`, `score_conformidade` (0–1, média das avaliações ÷ 5; `null` se não houver), `qtd_avaliacoes`, `endereco_id`, `cep`, `cidade`, `bairro`, `lat`, `lng`, `location` (point), `geo_chave` |
| `:EmpresaCompradora:Local` | `empresas` + `perfis` + `enderecos` | `id` (empresa_id), `nome`, campos de endereço como acima |
| `:PontoDeColeta:Local` | endereços de `rotas_enderecos` em rotas **ativas** | `id` (endereco_id), `nome` (nome_local), `tipo_local`, `logradouro`, `numero`, campos de endereço |
| `:Rota` | `rotas` ativas | `id` (rota_id), `coop_id`, `nome`, `esta_ativa`, `qtd_paradas`, `distancia_total_km` |
| `:Material` | `categorias_materiais` | `id` (categoria_id), `nome` (nome_categoria) |
| `:SyncRun` | — | registro de cada execução (contadores, duração). Mantém as 30 últimas |

> **Material = categoria.** A tabela `materiais` não tem nome: ela representa a *oferta* de uma
> cooperativa para uma categoria. Por isso o nó `Material` é a categoria (o mesmo nível de
> `empresa_materiais_interesses`).

### Relações

| Relação | Origem | Propriedades |
|---|---|---|
| `(Material)-[:SUBCATEGORIA_DE]->(Material)` | `categorias_materiais.categoria_pai_id` | — |
| `(Cooperativa)-[:PROCESSA_MATERIAL]->(Material)` | estoques, movimentações e ofertas | `capacidade_mensal_kg`, `estoque_atual_kg`, `preco_sugerido_medio`, `ofertado` |
| `(Cooperativa)-[:COLETA_DE]->(PontoDeColeta)` | `rotas` ativas + `rotas_enderecos` | `qtd_rotas`, `rotas` (nomes) |
| `(Cooperativa)-[:POSSUI_ROTA]->(Rota)` | `rotas` ativas | — |
| `(Rota)-[:PARADA]->(PontoDeColeta)` | `rotas_enderecos` | `ordem`, `nome_local`, `tipo_local`, `distancia_trecho_km`, `distancia_acumulada_km` |
| `(PontoDeColeta)-[:FORNECE_MATERIAL]->(Material)` | `triagens` → `coletas` → `rotas_enderecos` | `kg_estimado_periodo`, `qtd_coletas`, `estimado: true` |
| `(EmpresaCompradora)-[:INTERESSADA_EM]->(Material)` | `empresa_materiais_interesses` | — |
| `(EmpresaCompradora)-[:FAVORITOU]->(Cooperativa)` | `empresa_cooperativas_favoritas` | `desde` |
| `(Local)-[:CONECTADA_A]->(Local)` | calculada (ver abaixo) | `distancia_km`, `tipo` (`knn` ou `ponte`), `estimada: true` |

Tudo que é gravado recebe `sync_id`. `CONECTADA_A` é gravada numa direção só (id menor → id maior):
**consulte sem direção**, `-[:CONECTADA_A]-`.

### Métricas calculadas (não existem no Postgres)

| Métrica | Como é calculada | Limitação |
|---|---|---|
| `capacidade_mensal_kg` | soma das **entradas** de `movimentacoes_estoques` nos últimos `CAPACIDADE_JANELA_MESES` ÷ meses | é produção histórica, não capacidade declarada |
| `score_conformidade` | `AVG(avaliacoes.nota) / 5` | aceita `avaliado_id` = `perfil_id` **ou** `cooperativa_id` até o contrato da API Spring ser confirmado |
| `kg_estimado_periodo` | kg triado nos últimos `FORNECE_JANELA_DIAS`, **rateado igualmente** entre os pontos da rota | `coletas` aponta para a rota, não para o endereço |
| `CONECTADA_A.distancia_km` | linha reta (haversine) × `FATOR_TORTUOSIDADE`, ligando cada local aos `KNN_K` vizinhos mais próximos (até `MAX_ARESTA_KM`) + **pontes** entre grupos isolados, para o grafo ficar conexo | é estimativa; não é distância real por rua |

### Coordenadas

`enderecos` ainda **não tem latitude/longitude**. A ordem de resolução é:

1. colunas `enderecos.latitude` / `enderecos.longitude`, **se existirem** (o job detecta sozinho);
2. cache no Neo4j (`Local.geo_chave` → `lat`/`lng`), reaproveitado enquanto CEP, logradouro, número e cidade não mudarem;
3. BrasilAPI (CEP v2) e, se não achar, Nominatim/OpenStreetMap (no máximo 1 req/s).

Locais sem coordenada entram no grafo, mas **sem** `CONECTADA_A` (aparecem como `sem_coordenada` no log).
A solução definitiva é adicionar `latitude`/`longitude` em `enderecos` e geocodificar no cadastro (API Spring).

### Limpeza

Ao final de cada execução bem-sucedida, nós e relações **gerenciados** com `sync_id` antigo são apagados
(o que sumiu do Postgres some do grafo). **Não crie à mão** nós com os labels `Cooperativa`,
`EmpresaCompradora`, `PontoDeColeta` ou `Material`, nem as relações acima: eles serão removidos.
Se a extração não retornar nenhuma cooperativa, o job aborta antes de escrever, para não esvaziar o grafo
por engano (use `--permitir-vazio` se for intencional).

---

## Rodando localmente

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |   Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env    # Windows: copy .env.example .env  — e preencha

python -m rpa.sync.main --dry-run --sem-geocode   # testa a leitura do Postgres sem escrever nada
python -m rpa.sync.main --dry-run                 # + geocodificação
python -m rpa.sync.main                           # sync completo

pytest
```

O resultado sai em JSON (contagens extraídas, coordenadas, gravados, removidos, duração).

Recomendações de segurança:
- Use um **usuário somente leitura** no Postgres. O job também força `default_transaction_read_only`.
- No Neo4j, este serviço usa um usuário com escrita; a API de IA deve usar outro, **somente leitura**.

---

## Deploy na AWS

Para o EKS compartilhado, use [a orquestração por release](../orchestration/README.md).
Lambda é uma alternativa: não mantenha os dois agendamentos ativos.

### Alternativa: EventBridge Scheduler → Lambda

### 1. Gerar o pacote

Os wheels precisam ser de Linux, mesmo gerando no Windows:

```bash
pip install -r requirements.txt -t package \
    --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
cp -r rpa/sync rpa/handler.py package/
cd package && zip -r ../renovai-graph-sync.zip . && cd ..
```

No PowerShell (a partir da raiz do repositório):

```powershell
pip install -r requirements.txt -t package --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
Copy-Item -Recurse rpa\sync, rpa\handler.py package\
Compress-Archive -Path package\* -DestinationPath renovai-graph-sync.zip -Force
```

### 2. Criar a Lambda

- Runtime **Python 3.12**, arquitetura **x86_64**, handler `handler.lambda_handler`.
- Timeout: **5 min** (a primeira execução geocodifica tudo e pode demorar; as seguintes usam o cache).
- Memória: 256 MB basta.
- Variáveis de ambiente: as mesmas do `.env.example`. Para as senhas, prefira SSM Parameter Store ou Secrets Manager.
- Rede: veja a observação abaixo.

### 3. Agendar

EventBridge Scheduler → alvo: a Lambda → expressão `cron(0 6 * * ? *)` (03:00 em Brasília, todo dia).
Para rodar quando quiser: botão *Test* no console, ou

```bash
aws lambda invoke --function-name renovai-graph-sync --payload '{"dry_run": false}' saida.json
```

Evento aceito: `{"dry_run": bool, "sem_geocode": bool, "permitir_vazio": bool}`.

### 4. Monitorar

Os logs vão para o CloudWatch. Crie um alarme na métrica `Errors` da função (ou um destino *on failure*
para SNS, que manda e-mail). Cada execução também fica registrada no próprio grafo:

```cypher
MATCH (s:SyncRun) RETURN s ORDER BY s.iniciado_em DESC LIMIT 5
```

### Rede

A Lambda precisa alcançar o Postgres **e** o Neo4j (e as APIs de geocodificação).
Se o Postgres for um RDS em VPC privada, a Lambda precisa estar na VPC, e aí só sai para a internet
(Neo4j Aura, BrasilAPI, Nominatim) com um NAT Gateway, que é pago. Alternativas mais baratas:
Neo4j self-hosted na mesma VPC + coordenadas vindas do Postgres (`GEOCODERS=none`),
ou rodar o job como cron numa instância EC2 que já tenha acesso aos dois.

---

## Estrutura

```
sync/
  config.py      variáveis de ambiente / .env
  extract.py     queries no Postgres (schema oficial do Renovaí)
  geocode.py     CEP → lat/lng (BrasilAPI, Nominatim) + cache
  transform.py   funções puras: tipos, malha k-vizinhos, payload
  load.py        UNWIND/MERGE no Neo4j, limpeza, registro de execução
  main.py        orquestração e CLI
handler.py       entrada da Lambda
queries/         query de recomendação consumida pela IA (referência do contrato)
notebooks/       exploração (importa de sync/, não duplica código)
tests/           testes das funções puras
```
