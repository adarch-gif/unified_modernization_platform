# Unified Modernization Platform

Implementation starter for migrating:

- **Azure AI Search → Elasticsearch**
- **Azure SQL / Cosmos DB → GCP Spanner / Firestore / AlloyDB**

This is a production-grade starter, not a finished migration. It provides the core platform contracts and reusable components that stay stable while domain-level discovery continues.

---

## Documentation Map

Read the documents in the order that matches your role. Each document is self-contained; you do not need to read preceding ones first.

| # | Document | Audience | What it covers |
|---|----------|----------|----------------|
| 1 | [`docs/EXECUTIVE_GUIDE.md`](docs/EXECUTIVE_GUIDE.md) | Business stakeholders, executives | Business problem, solution, migration phases, risk management, governance |
| 2 | [`TECHNICAL_PRIMER.md`](TECHNICAL_PRIMER.md) | Product owners, operations leads, TPMs | System components, data flows, traffic modes, configuration reference, monitoring signals, runbooks |
| 3 | [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) | Engineers onboarding to the codebase | Module layout, runtime surfaces, config surfaces, local dev, test suite |
| 4 | [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md) | Principal engineers, architects | Every significant design decision — context, alternatives rejected, rationale, trade-offs, long-term consequences |
| 5 | [`docs/TERRAFORM_DEPLOYMENT.md`](docs/TERRAFORM_DEPLOYMENT.md) | Engineers, DevOps | GCP infrastructure, Terraform variables, pilot deployment steps |
| 6 | [`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md) | Engineering leads, TPMs | What remains to be built, phase-by-phase |
| 7 | [`docs/DOMAIN_ONBOARDING_TEMPLATE.md`](docs/DOMAIN_ONBOARDING_TEMPLATE.md) | Domain leads, engineers | Checklist for onboarding a new data domain |

### Legacy redirect files

These files exist only to preserve older links. Do not treat them as sources of truth.

| File | Points to |
|------|-----------|
| `PLATFORM_OVERVIEW.md` | → `docs/EXECUTIVE_GUIDE.md` |
| `docs/PRODUCT_OPERATIONS_PRIMER.md` | → `TECHNICAL_PRIMER.md` |
| `docs/ADR_COMPENDIUM.md` | → `ARCHITECTURE_DECISIONS.md` |
| `docs/ARCHITECTURE.md` | → `DEVELOPER_GUIDE.md` + `ARCHITECTURE_DECISIONS.md` |

---

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

## What is not implemented yet

- Real Azure, GCP, or Elasticsearch credentials and deployed runtime wiring
- Domain-specific schemas and mappings
- Full consumer-specific OData parity
- Fully automated multi-environment CI/CD rollout pipelines
- Remaining domain-specific CDC envelopes and source-specific enrichments beyond the built-in Cosmos, Firestore, Debezium-style, and Spanner adapter set
- Final Spanner versus Firestore versus AlloyDB decisions by domain

---

## Repository Layout

```text
src/unified_modernization/
  adapters/         Source adapter interfaces and helpers
  backfill/         Bulk side-load and stream handoff planning
  config/           YAML-driven domain onboarding loader
  contracts/        Canonical event and projection models
  cutover/          Backend and search cutover state machines
  gateway/          Search Gateway logic and ASGI app
  observability/    Telemetry protocol and OTLP bridge
  projection/       Projection builder, runtime, publisher, and state stores
  reconciliation/   Reconciliation models and comparison logic
  routing/          Tenant routing policy engine

docs/
  EXECUTIVE_GUIDE.md          ← doc 1
  TERRAFORM_DEPLOYMENT.md     ← doc 5
  IMPLEMENTATION_ROADMAP.md   ← doc 6
  DOMAIN_ONBOARDING_TEMPLATE.md ← doc 7

TECHNICAL_PRIMER.md           ← doc 2
DEVELOPER_GUIDE.md            ← doc 3
ARCHITECTURE_DECISIONS.md     ← doc 4

examples/
  domain_config.yaml
  gateway_runtime.env.template

infra/terraform/              GCP infrastructure for pilot deployments

tests/
  Projection, gateway, cutover, reconciliation, and observability tests
```

---

## Local Usage

### Install

```bash
python -m pip install -e .[dev]
```

### Run tests

```bash
python -m pytest -q
```

### Run the translation-only ASGI app

```bash
uvicorn unified_modernization.gateway.asgi:app --app-dir src --reload
```

### Run the full HTTP search gateway

```bash
uvicorn unified_modernization.gateway.http_api:app --app-dir src --reload
```

### Run the gateway smoke/load harness

```bash
python -m unified_modernization.gateway.harness \
  --cases-file examples/search_harness_cases.jsonl \
  --concurrency 8 \
  --iterations 20
```

The harness expects the concrete client env vars to be set — start from [`examples/gateway_runtime.env.template`](examples/gateway_runtime.env.template).

Or use the Windows wrapper that writes timestamped artifacts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_gateway_harness.ps1 `
  -EnvFile gateway_runtime.env.local `
  -CasesFile examples/search_harness_cases.jsonl `
  -Concurrency 8 `
  -Iterations 20
```

### Health check

```
GET /health
```

### Translate OData to Elasticsearch DSL

```
POST /translate
{ "params": { "$search": "gold customer", "$filter": "Status eq 'ACTIVE'", "$top": "10" } }
```

---

## Design Position

One unified modernization backbone, but backend-primary cutover and search-serving cutover are independent.

- one canonical event plane
- one projection and reconciliation model
- one Search Gateway
- one replay and cutover control model

But:

- backend cutover is independent per domain
- search cutover is independent per domain
- target-store decisions remain domain-specific

---

## Immediate Next Steps

1. Wire `SpannerProjectionStateStore`, `FirestoreCutoverStateStore`, and the concrete search clients to real cloud credentials.
2. Run the gateway harness against real Azure and Elastic endpoints at target and 2× target concurrency; keep the report artifacts as rollout evidence.
3. Switch telemetry from `memory`/`logger` to `otlp_http` in deployed environments.
4. Replace local pilot-grade durability layers with managed production stores and secret-provider integration.
5. Add remaining domain-specific CDC adapters not covered by the built-in Cosmos, Firestore, Debezium-style, and Spanner adapters.

See [`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md) for the full phase-by-phase plan.

---

## Status

Working starter repository. Intended to accelerate implementation and reduce design drift while domain discovery continues.
