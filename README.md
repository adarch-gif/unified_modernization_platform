# Unified Modernization Platform

Implementation starter for a unified modernization initiative that migrates:

- Azure AI Search to Elasticsearch
- Azure SQL Database and Azure Cosmos DB to GCP operational stores

This repository is intentionally designed as a production-grade starter, not a fake "finished migration." It provides the core platform contracts and reusable components that should remain stable even while domain-level discovery is still incomplete.

## What is implemented

- Canonical domain event contract
- Projection builder with incomplete-projection handling
- Domain-scoped dependency policy resolution for shared entity types across multiple domains
- Atomic projection mutation through a pluggable projection state store
- Pluggable projection state store with durable SQLite implementation
- Spanner-backed projection state store contract and schema-driven implementation path
- Independent backend and search cutover state machines
- Persisted cutover transition stores with restart-safe rehydration, including JSONL and Firestore-backed paths
- Tenant routing policy engine and alias-routing model
- Whale-tenant ingestion partition policy
- Search Gateway service and OData to Elasticsearch translator
- ASGI gateway middleware with API-key protection and request-size guard
- ASGI app builder with production-aware auth, bounded request parsing, and explicit `422` OData translation failures
- Search evaluation harness with live overlap and offline judged relevance metrics
- Operational shadow quality gate with telemetry-ready events and canary auto-freeze on judged regression
- Best-effort shadow and canary comparison paths that do not fail the primary request on shadow-only errors
- Resilient search backend wrapper with timeout, retry, and circuit-breaker primitives
- Gateway bootstrap that makes resilient wrappers and telemetry explicit at startup
- Backfill coordinator with side-load to stream-handoff planning
- Backfill checkpointing support for resumable bulk runs
- Firestore outbox normalization model
- Domain config loader for YAML-driven onboarding
- Reconciliation engine with tenant, cohort, and delete-aware validation
- Recursive bucketed anti-entropy reconciliation for hash-first drift detection
- Projection runtime wrapper with backpressure and dead-letter handling
- Priority-aware backpressure bypass for repair and pending-entity completion traffic
- Observability primitives for structured events, counters, timings, and spans
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
  config/           YAML-driven domain onboarding loader
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
  Bulk side-load, typed watermarks, resumable checkpoints, and stream handoff planning
- `cutover/state_machine.py`
  Persisted transition events with restart-safe cutover state rehydration across JSONL and Firestore-backed stores
- `cutover/bootstrap.py`
  Production-safe cutover bootstrap that forbids ephemeral in-memory state outside local/dev/test
- `config/loader.py`
  YAML loader that turns domain onboarding config into runtime dependency policies
- `gateway/evaluation.py`
  Live overlap metrics and offline judged relevance metrics such as `NDCG@10` and `MRR`
- `gateway/service.py`
  Canary-aware search flow that can automatically freeze Elastic ramp-up when judged shadow quality regresses
- `gateway/asgi.py`
  API-key middleware, request-size limits, explicit `422` translation errors, and a testable ASGI app builder
- `gateway/resilience.py`
  Resilient backend wrapper for timeout, retry, and circuit-breaker behavior
- `gateway/bootstrap.py`
  Production-safe gateway startup that wraps raw backends and rejects silent telemetry in non-dev environments
- `observability/telemetry.py`
  Structured telemetry events, counters, timings, and trace-like spans
- `reconciliation/engine.py`
  Snapshot reconciliation plus recursive bucketed anti-entropy, remote paginated bucket fetch, and bucket-level drill-down
- `routing/tenant_policy.py`
  Shared-index versus dedicated-index alias routing policy plus whale-tenant ingestion partitioning
- `projection/bootstrap.py`
  Runtime bootstrap helper that forbids in-memory projection state outside local/dev/test
- `projection/runtime.py`
  Backpressure and DLQ wrapper around projection processing

## Immediate next steps

1. Wire `SpannerProjectionStateStore` to a real `google-cloud-spanner` database in non-local environments and provision the published DDL.
2. Add real source adapters for Azure SQL, Cosmos, Spanner, Firestore outbox, and AlloyDB CDC.
3. Add live Azure Search and Elasticsearch query clients behind the gateway and a real Elasticsearch index writer with external versioning.
4. Route telemetry into OpenTelemetry exporters or a real metrics backend instead of in-memory/logger sinks.
5. Replace local pilot-grade durability layers with managed production stores and secret-provider integration where required.

## Status

This is a working starter repository intended to accelerate implementation and reduce design drift while discovery continues.
