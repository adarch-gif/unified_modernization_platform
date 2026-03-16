# Implementation Roadmap

## Phase 0

- inventory domains
- decide target store per domain
- export Azure Search contracts
- capture query logs
- define tenant rollout order

## Phase 1

- wire the implemented Spanner-backed projection state store to real GCP environments
- implement real source adapters
- implement real Elasticsearch write client
- implement gateway clients for Azure Search and Elasticsearch
- wire tenant alias routing and ingestion partition policies into live gateway and ingestion clients
- wire resilient backend wrappers and real telemetry exporters into live gateway startup
- implement batch side-load path, checkpoint persistence, and source-watermark capture for backfill
- replace pilot cutover JSONL durability with a managed cutover state store

## Phase 2

- add replay and production DLQ handling
- add reconciliation by entity cohort and tenant
- wire remote reconciliation stores with streamed digest APIs and deeper Merkle validation
- integrate the existing domain onboarding config loader into deployed services
- add OpenTelemetry exporters and dashboards
- add judged query corpus and offline relevance evaluation workflow
- route quality-gate events into production alerting and canary automation controls

## Phase 3

- deploy thin slice
- run shadow traffic
- collect overlap, `NDCG@10`, `MRR`, and zero-result evidence
- exercise rollback

## Phase 4

- domain-wave rollout
- tenant-based canaries
- decommission legacy search paths
