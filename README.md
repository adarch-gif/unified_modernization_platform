# Unified Modernization Platform

Implementation starter for a unified modernization initiative that migrates:

- Azure AI Search to Elasticsearch
- Azure SQL Database and Azure Cosmos DB to GCP operational stores

This repository is intentionally designed as a production-grade starter, not a fake "finished migration." It provides the core platform contracts and reusable components that should remain stable even while domain-level discovery is still incomplete.

## What is implemented

- Canonical domain event contract
- Projection builder with incomplete-projection handling
- Pluggable projection state store with durable SQLite implementation
- Spanner-backed projection state store contract and schema-driven implementation path
- Independent backend and search cutover state machines
- Tenant routing policy engine and alias-routing model
- Search Gateway service and OData to Elasticsearch translator
- Search evaluation harness with live overlap and offline judged relevance metrics
- Operational shadow quality gate with telemetry-ready events
- Resilient search backend wrapper with timeout, retry, and circuit-breaker primitives
- Backfill coordinator with side-load to stream-handoff planning
- Firestore outbox normalization model
- Reconciliation engine with tenant, cohort, and delete-aware validation
- Bucketed anti-entropy reconciliation for hash-first drift detection
- Example config and implementation roadmap
- Unit tests for the highest-risk logic

## What is not implemented yet

- Real Azure, GCP, or Elasticsearch credentials and runtime integration
- Domain-specific schemas and mappings
- Full consumer-specific OData parity
- Production IaC for all environments
- Final Spanner versus Firestore versus AlloyDB decisions by domain

## Repository layout

```text
src/unified_modernization/
  adapters/         Source adapter interfaces and helpers
  contracts/        Canonical event and projection models
  cutover/          Backend and search cutover state machines
  gateway/          Search Gateway logic and ASGI app
  projection/       Projection builder and state handling
  reconciliation/   Reconciliation models and comparison logic
  routing/          Tenant routing policy engine
docs/
  ARCHITECTURE.md
  IMPLEMENTATION_ROADMAP.md
  DOMAIN_ONBOARDING_TEMPLATE.md
examples/
  domain_config.yaml
tests/
  Projection, gateway, cutover, and reconciliation tests
```

## Local usage

### Install in editable mode

```powershell
python -m pip install -e .[dev]
```

### Run tests

```powershell
python -m pytest -q
```

### Run the lightweight ASGI app

```powershell
uvicorn unified_modernization.gateway.asgi:app --app-dir src --reload
```

### Health endpoint

```text
GET /health
```

### Translate OData to Elasticsearch DSL

```text
POST /translate
{
  "params": {
    "$search": "gold customer",
    "$filter": "Status eq 'ACTIVE'",
    "$top": "10"
  }
}
```

## Design position

This repo follows one architectural rule above all others:

Build one unified modernization backbone, but keep backend-primary cutover and search-serving cutover independent.

That means:

- one canonical event plane
- one projection and reconciliation model
- one Search Gateway
- one replay and cutover control model

But:

- backend cutover remains independent by domain
- search cutover remains independent by domain
- target-store decisions remain domain-specific

## New production-grade seams in this repo

- `projection/store.py`
  Durable control-plane seam with `InMemoryProjectionStateStore`, `SqliteProjectionStateStore`, and `SpannerProjectionStateStore`
- `backfill/coordinator.py`
  Bulk side-load and stream handoff planner so historical backfill does not depend on the real-time bus
- `gateway/evaluation.py`
  Live overlap metrics and offline judged relevance metrics such as `NDCG@10` and `MRR`
- `gateway/resilience.py`
  Resilient backend wrapper for timeout, retry, and circuit-breaker behavior
- `reconciliation/engine.py`
  Snapshot reconciliation plus bucketed anti-entropy and bucket-level drill-down
- `routing/tenant_policy.py`
  Shared-index versus dedicated-index alias routing policy
- `projection/bootstrap.py`
  Runtime bootstrap helper that forbids in-memory projection state outside local/dev/test

## Immediate next steps

1. Wire `SpannerProjectionStateStore` to a real `google-cloud-spanner` database in non-local environments and provision the published DDL.
2. Add real source adapters for Azure SQL, Cosmos, Spanner, Firestore outbox, and AlloyDB CDC.
3. Extend bucketed reconciliation into a multi-level Merkle-style validation workflow for billion-document domains.
4. Add end-to-end replay, DLQ handling, and rehydration workers for pending projections.
5. Add live Azure Search and Elasticsearch query clients behind the gateway, then wire the resilient backend wrapper into production startup and telemetry pipelines.

## Status

This is a working starter repository intended to accelerate implementation and reduce design drift while discovery continues.
