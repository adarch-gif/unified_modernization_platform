# Implementation Roadmap

## Phase 0

- inventory domains
- decide target store per domain
- export Azure Search contracts
- capture query logs
- define tenant rollout order

## Phase 1

- implement Spanner-backed projection state store from the durable store contract
- implement real source adapters
- implement real Elasticsearch write client
- implement gateway clients for Azure Search and Elasticsearch
- wire tenant alias routing into live gateway clients
- implement batch side-load path and source-watermark capture for backfill

## Phase 2

- add replay and DLQ handling
- add reconciliation by entity cohort and tenant
- add domain onboarding config loader
- add metrics and tracing
- add judged query corpus and offline relevance evaluation workflow

## Phase 3

- deploy thin slice
- run shadow traffic
- collect overlap, `NDCG@10`, `MRR`, and zero-result evidence
- exercise rollback

## Phase 4

- domain-wave rollout
- tenant-based canaries
- decommission legacy search paths
