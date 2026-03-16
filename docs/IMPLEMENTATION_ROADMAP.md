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
- implement batch side-load path and source-watermark capture for backfill

## Phase 2

- add replay and production DLQ handling
- add reconciliation by entity cohort and tenant
- back remote reconciliation with streamed digest APIs and deeper Merkle validation
- add domain onboarding config loader
- add OpenTelemetry exporters and dashboards
- add judged query corpus and offline relevance evaluation workflow
- route quality-gate events into production alerting

## Phase 3

- deploy thin slice
- run shadow traffic
- collect overlap, `NDCG@10`, `MRR`, and zero-result evidence
- exercise rollback

## Phase 4

- domain-wave rollout
- tenant-based canaries
- decommission legacy search paths
