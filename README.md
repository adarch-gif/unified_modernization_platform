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
- HTTP gateway app that exposes the routed `SearchGatewayService` on `/search`
- Search evaluation harness with live overlap and offline judged relevance metrics
- Operational shadow quality gate with telemetry-ready events and canary auto-freeze on judged regression
- Best-effort shadow and canary comparison paths that do not fail the primary request on shadow-only errors
- Resilient search backend wrapper with timeout, retry, and circuit-breaker primitives
- Gateway bootstrap that makes resilient wrappers and telemetry explicit at startup
- Backfill coordinator with side-load to stream-handoff planning
- Backfill checkpointing support for resumable bulk runs
- Firestore outbox normalization model
- Cosmos DB change feed normalization adapter
- Debezium-style CDC normalization adapter for Azure SQL and AlloyDB event streams
- Spanner change-stream normalization adapter
- Domain config loader for YAML-driven onboarding
- Reconciliation engine with tenant, cohort, and delete-aware validation
- Recursive bucketed anti-entropy reconciliation for hash-first drift detection
- Projection runtime wrapper with backpressure and dead-letter handling
- Priority-aware backpressure bypass for repair and pending-entity completion traffic
- Azure AI Search and Elasticsearch HTTP query clients
- Elasticsearch document publisher with external versioning and tenant-aware alias routing
- Optional projection runtime publisher hook for search-index delivery and replay on publish failure
- Observability primitives for structured events, counters, timings, and spans
- OpenTelemetry-compatible telemetry sink with OTLP HTTP export bootstrap
- Gateway smoke/load harness with JSON or JSONL case playback and latency/error reporting
- Docker packaging for Cloud Run deployment
- Terraform stack for pilot-grade GCP infrastructure deployment
- Example config and implementation roadmap
- Unit tests for the highest-risk logic

## What is not implemented yet

- Real Azure, GCP, or Elasticsearch credentials and deployed runtime wiring
- Domain-specific schemas and mappings
- Full consumer-specific OData parity
- Fully automated multi-environment CI/CD rollout pipelines
- Remaining domain-specific CDC envelopes and source-specific enrichments beyond the built-in Cosmos, Firestore, Debezium-style, and Spanner adapter set
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
  TERRAFORM_DEPLOYMENT.md
examples/
  domain_config.yaml
infra/
  terraform/        GCP deployment stack for pilot environments
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

### Run the translation-only ASGI app

```powershell
uvicorn unified_modernization.gateway.asgi:app --app-dir src --reload
```

### Run the full HTTP search gateway locally

```powershell
uvicorn unified_modernization.gateway.http_api:app --app-dir src --reload
```

### Run the gateway smoke/load harness

```powershell
python -m unified_modernization.gateway.harness `
  --cases-file examples/search_harness_cases.jsonl `
  --concurrency 8 `
  --iterations 20
```

This command reuses the real gateway bootstraps and expects the concrete client env vars to be set, for example:

```text
UMP_ENVIRONMENT=prod
UMP_GATEWAY_MODE=shadow
UMP_AZURE_SEARCH_ENDPOINT=https://<service>.search.windows.net
UMP_AZURE_SEARCH_DEFAULT_INDEX=customer-search
UMP_ELASTICSEARCH_ENDPOINT=https://<elastic-cluster>
UMP_ELASTICSEARCH_DEFAULT_INDEX=customerdocument-shared_a-read
UMP_GATEWAY_FIELD_MAP=Status=status,Tier=tier,Region=region
UMP_TELEMETRY_MODE=memory
```

If you want a timestamped artifact automatically written under `artifacts/gateway_harness`, use the wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_gateway_harness.ps1 `
  -EnvFile gateway_runtime.env.local `
  -CasesFile examples/search_harness_cases.jsonl `
  -Concurrency 8 `
  -Iterations 20
```

Start from [gateway_runtime.env.template](examples/gateway_runtime.env.template) and fill in the real endpoint and credential values locally.

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
- `gateway/clients.py`
  Concrete Azure AI Search and Elasticsearch query backends that fit the gateway `SearchBackend` protocol
- `gateway/bootstrap.py`
  Production-safe gateway startup plus config-driven construction for the concrete Azure and Elasticsearch clients
- `gateway/harness.py`
  Smoke/load runner for the concrete gateway path with concurrency, latency percentiles, and shadow-signal reporting
- `gateway/asgi.py`
  API-key middleware, request-size limits, explicit `422` translation errors, and a testable ASGI app builder
- `gateway/http_api.py`
  Deployable HTTP surface for `SearchGatewayService`, including `/search`, `/translate`, and `/health`
- `gateway/resilience.py`
  Resilient backend wrapper for timeout, retry, and circuit-breaker behavior
- `observability/telemetry.py`
  Structured telemetry events, counters, timings, and trace-like spans
- `observability/opentelemetry.py`
  OpenTelemetry-compatible telemetry sink that maps the platform telemetry protocol to OTLP-ready traces and metrics
- `observability/bootstrap.py`
  Environment-driven telemetry sink selection for noop, memory, logger, or OTLP HTTP export modes
- `scripts/run_gateway_harness.ps1`
  Windows-friendly wrapper that loads env, runs the harness, and writes timestamped rollout evidence to `artifacts/`
- `reconciliation/engine.py`
  Snapshot reconciliation plus recursive bucketed anti-entropy, remote paginated bucket fetch, and bucket-level drill-down
- `routing/tenant_policy.py`
  Shared-index versus dedicated-index alias routing policy plus whale-tenant ingestion partitioning
- `projection/bootstrap.py`
  Runtime bootstrap helper that forbids in-memory projection state outside local/dev/test and can build the Elasticsearch publisher from config
- `projection/runtime.py`
  Backpressure, DLQ handling, and optional search-index publishing around projection processing
- `projection/publisher.py`
  Elasticsearch publisher with external versioning, tenant-aware alias routing, and bulk indexing support
- `infra/terraform`
  GCP infrastructure stack for Cloud Run, Spanner, Firestore, Pub/Sub, Artifact Registry, and Secret Manager

## Immediate next steps

1. Wire `SpannerProjectionStateStore`, `FirestoreCutoverStateStore`, and the concrete search clients to real cloud credentials and managed environments.
2. Run the gateway harness against real Azure and Elastic endpoints at target and 2x target concurrency, then keep the report artifacts as rollout evidence.
3. Switch telemetry mode from memory/logger to OTLP HTTP or a production metrics backend in deployed environments.
4. Replace local pilot-grade durability layers with managed production stores and secret-provider integration where required.
5. Add any remaining domain-specific CDC envelopes and source-specific enrichments not covered by the built-in Cosmos, Firestore, Debezium-style, and Spanner adapters.

## Status

This is a working starter repository intended to accelerate implementation and reduce design drift while discovery continues.
