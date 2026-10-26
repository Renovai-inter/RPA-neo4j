# RPA Neo4j — execução e deploy AWS

O CronJob executa diariamente às 03h de Brasília no EKS compartilhado das APIs.
O processo sincroniza PostgreSQL com Neo4j e termina, sem Service ou Ingress.

## Preparação

1. Crie o ECR `renovai-rpa-neo4j` na região do EKS (Kubernetes 1.27+).
2. Garanta acesso dos nodes ao ECR, PostgreSQL, Neo4j e geocodificadores.
   Use usuário PostgreSQL somente leitura e um usuário Neo4j de escrita próprio.
3. Desative qualquer agendamento Lambda/EventBridge anterior e aguarde a execução
   terminar. Dois sincronizadores simultâneos podem prejudicar a limpeza do grafo.
4. Crie namespace e Secret, preenchendo os valores antes de aplicar:

```bash
kubectl create namespace renovai-api --dry-run=client -o yaml | kubectl apply -f -
cp orchestration/secret.template.yaml orchestration/secret.yaml
# Preencha orchestration/secret.yaml com as credenciais reais.
kubectl apply -f orchestration/secret.yaml
```

Não versione `secret.yaml` nem `.env`. Outras opções de `.env.example` podem ser
adicionadas ao Secret. O manifesto usa `IA_API_URL=http://ia-api-service` no mesmo
namespace. O retreino fica habilitado; o código atual registra falhas dele sem
fazer a sincronização retornar erro. Confira a origem antes da primeira escrita:
dados gerenciados que desapareceram do PostgreSQL serão removidos do Neo4j.

## GitHub Actions

Configure os Secrets `AWS_REGION`, `EKS_CLUSTER_NAME`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` e `AWS_SESSION_TOKEN` (credenciais temporárias).
A variável `ECR_REPOSITORY` é opcional. A identidade precisa publicar no ECR,
consultar EKS e ter acesso Kubernetes para ler Secrets e aplicar namespace/CronJob.
O runner precisa alcançar o endpoint do cluster.

Publique uma release estável: a pipeline valida os arquivos, publica a imagem com
o SHA do commit da tag e atualiza o CronJob. Push, PR, rascunho e pré-release não
disparam produção. A sincronização só inicia no próximo horário; Jobs ativos
continuam com a imagem anterior. O CI valida código, testes e imagem em push/PR.

## Operação

`concurrencyPolicy: Forbid` bloqueia sobreposição dos Jobs deste CronJob, mas não
de execuções manuais ou de outras Lambdas/CronJobs. Não há retry automático após
falha. O prazo é de uma hora por Job. Requests de 100m/256Mi e limites de 1 CPU/512Mi
são valores iniciais: meça o consumo real e ajuste antes de produção.
Aplicar o manifesto confirma o agendamento, não o sucesso de uma sincronização.

```bash
kubectl get cronjob,jobs,pods -n renovai-api
kubectl logs -n renovai-api job/NOME_DO_JOB
# Pausar próximas execuções, sem interromper Jobs ativos:
kubectl patch cronjob rpa-neo4j -n renovai-api -p '{"spec":{"suspend":true}}'
```

Para retomar, use o patch com `false`. Para validar sem escrita, pause o
agendamento, aguarde Jobs ativos terminarem e use `--dry-run --sem-geocode`.

## Local

```bash
cp .env.example .env
# Preencha com bancos de teste acessíveis ao container.
docker compose run --rm --build rpa-neo4j python -m rpa.sync.main --dry-run --sem-geocode
docker compose run --rm rpa-neo4j
```

O segundo comando escreve no Neo4j e pode disparar retreino. No Docker Desktop,
use `host.docker.internal` para bancos no Windows; localhost é o próprio container.

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
