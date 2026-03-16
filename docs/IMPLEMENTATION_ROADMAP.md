# Implementation Roadmap

## Phase 0

- inventory domains
- decide target store per domain
- export Azure Search contracts
- capture query logs
- define tenant rollout order

## Phase 1

- wire the implemented Spanner-backed projection state store to real GCP environments
- complete any remaining real source adapters or enrichers beyond the built-in Firestore outbox, Cosmos change feed, Debezium-style CDC, and Spanner change-stream paths
- wire the implemented Elasticsearch write client to live aliases and secret-managed credentials
- wire the implemented gateway clients for Azure Search and Elasticsearch to live credentials and domain mappings
- wire tenant alias routing and ingestion partition policies into live gateway and ingestion clients
- wire resilient backend wrappers and real telemetry exporters into live gateway startup
- implement batch side-load path, checkpoint persistence, and source-watermark capture for backfill
- replace pilot cutover JSONL durability with the implemented Firestore cutover store in deployed environments

## Phase 2

- add replay and production DLQ handling
- add reconciliation by entity cohort and tenant
- wire remote reconciliation stores with streamed digest APIs and deeper Merkle validation
- integrate the existing domain onboarding config loader into deployed services
- add OpenTelemetry exporters and dashboards
- add judged query corpus and offline relevance evaluation workflow
- route quality-gate events into production alerting and canary automation controls
- wire the ASGI surface to real auth and secret management inputs in deployed environments

## Phase 3

- deploy thin slice
- run the gateway smoke/load harness against deployed Azure and Elastic backends
- run shadow traffic
- collect overlap, `NDCG@10`, `MRR`, and zero-result evidence
- exercise rollback

## Phase 4

- domain-wave rollout
- tenant-based canaries
- decommission legacy search paths
