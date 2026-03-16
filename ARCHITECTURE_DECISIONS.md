# Unified Modernization Platform - Architecture Decisions

> Canonical ADR set for the current repo.
> Purpose: explain the design choices that shape the implementation that exists today, not an aspirational platform.

## ADR-001: Canonical Event Contract

**Decision**

Every source adapter normalizes raw vendor events into `CanonicalDomainEvent` before handing work to the rest of the platform.

**Why**

- isolates downstream code from source-specific envelopes
- centralizes ordering, provenance, and change semantics
- enables multiple CDC technologies to share one projection runtime

**Current code anchors**

- `src/unified_modernization/contracts/events.py`
- `src/unified_modernization/adapters/`

## ADR-002: Fragment-Based Projection Assembly

**Decision**

Build entity projections from independently arriving fragments rather than requiring full-document CDC.

**Why**

- matches real source-system boundaries
- supports partial availability and freshness rules
- enables deterministic completeness decisions per domain

**Current code anchors**

- `src/unified_modernization/projection/builder.py`
- `src/unified_modernization/contracts/projection.py`
- `src/unified_modernization/config/loader.py`

## ADR-003: Atomic State Mutation at the Store Boundary

**Decision**

State stores own the transaction boundary via `mutate_entity(...)`.

**Why**

- prevents read-outside-write-window races
- lets each store enforce its own concurrency guarantees
- keeps builder logic deterministic across InMemory, SQLite, and Spanner

**Current code anchors**

- `src/unified_modernization/projection/store.py`

## ADR-004: Native Source Ordering Beats Timestamp Ordering

**Decision**

Prefer native source versions such as LSNs and record sequences over timestamps. Timestamp fallback exists only as an explicit opt-in escape hatch.

**Why**

- timestamps are weaker under clock skew and same-tick writes
- ordering bugs silently corrupt search projections
- source-native ordering better matches at-least-once delivery reality

**Current code anchors**

- `src/unified_modernization/adapters/debezium_cdc.py`
- `src/unified_modernization/adapters/spanner_change_stream.py`
- `src/unified_modernization/adapters/cosmos_change_feed.py`

## ADR-005: Dual-Track Cutover State Machines

**Decision**

Backend migration state and search-serving migration state are modeled as independent FSM tracks.

**Why**

- backend and search can progress at different speeds
- rollback needs are different
- coupling them would create illegal or ambiguous combined states

**Current code anchors**

- `src/unified_modernization/cutover/state_machine.py`

## ADR-006: Firestore for Durable Cutover State

**Decision**

Use Firestore for production cutover persistence, with append-only events plus a transactional latest-state snapshot.

**Why**

- cutover write volume is tiny relative to projection state
- transactional snapshot updates are sufficient
- keeping cutover separate from projection Spanner reduces blast radius

**Current code anchors**

- `src/unified_modernization/cutover/state_machine.py`
- `infra/terraform/firestore.tf`

## ADR-007: External Versioning in Elasticsearch

**Decision**

Projection publishing uses `version_type=external` with platform-owned `projection_version`.

**Why**

- avoids read-before-write
- prevents stale writes from silently overwriting newer documents
- makes publish version meaningful at the business level

**Current repo behavior**

- a `409` is currently treated as a publish failure and routed to the application DLQ for review
- this is operationally conservative even though the target may already contain a newer version

**Current code anchors**

- `src/unified_modernization/projection/publisher.py`
- `src/unified_modernization/projection/runtime.py`

## ADR-008: Translation Surface Separate from Search Routing

**Decision**

Keep OData translation and routed search orchestration conceptually separate.

**Why**

- translation-only use cases remain simple
- routing logic stays isolated from the minimal ASGI middleware surface
- the same `SearchGatewayService` can still be embedded or hosted behind different transports

**Current repo shape**

- `asgi.py` is translation-only
- `http_api.py` is the deployable HTTP gateway that hosts `SearchGatewayService`

**Current code anchors**

- `src/unified_modernization/gateway/asgi.py`
- `src/unified_modernization/gateway/http_api.py`
- `src/unified_modernization/gateway/service.py`

## ADR-009: Application-Level Backpressure with Bypass Conditions

**Decision**

Backpressure is enforced in the projection runtime, not only at the broker, and includes bypass conditions.

**Why**

- lets the platform prioritize corrective work
- avoids deadlocking incomplete entities
- makes throttling decisions based on platform state, not only queue depth

**Current bypass reasons**

- `priority_change_type`
- `pending_entity_completion`

**Current code anchors**

- `src/unified_modernization/projection/runtime.py`

## ADR-010: Runtime-Level DLQ and Quarantine

**Decision**

DLQ and quarantine are application concerns, not broker-only concerns.

**Why**

- the platform needs rich failure context
- broker-native DLQs do not capture projection state semantics
- quarantine prevents endless reprocessing of poison entities

**Current code anchors**

- `src/unified_modernization/projection/runtime.py`
- `src/unified_modernization/projection/builder.py`

## ADR-011: Bucketed Anti-Entropy Reconciliation

**Decision**

Use bucketed hash-first reconciliation before document-level comparison.

**Why**

- healthy runs are cheap
- drifted buckets can be isolated without full corpus transfer
- this scales better than naive full-document scans

**Current code anchors**

- `src/unified_modernization/reconciliation/engine.py`

## ADR-012: Per-Backend Resilience Wrapper

**Decision**

Wrap each search backend in retries, timeouts, and a circuit breaker.

**Why**

- backend failures should not collapse the whole search surface
- resilience policy should be explicit and testable
- the gateway needs different timeouts and recovery behavior by backend

**Current code anchors**

- `src/unified_modernization/gateway/resilience.py`
- `src/unified_modernization/gateway/bootstrap.py`

## ADR-013: Shadow Quality Gate with Judged Relevance

**Decision**

Use overlap plus judged metrics such as NDCG and MRR to decide whether canary behavior is safe.

**Why**

- result overlap alone is not enough
- migration risk is in search quality, not just HTTP correctness
- judged relevance provides a more defensible release gate

**Current code anchors**

- `src/unified_modernization/gateway/evaluation.py`
- `src/unified_modernization/gateway/service.py`

## ADR-014: Tenant-Aware Routing and Whale Isolation

**Decision**

Route large tenants to dedicated Elasticsearch aliases and keep smaller tenants in shared pools with deterministic routing.

**Why**

- reduces noisy-neighbor effects
- supports tenant-specific scaling where needed
- keeps the shared estate efficient for the long tail

**Current code anchors**

- `src/unified_modernization/routing/tenant_policy.py`

## ADR-015: Observability as a Protocol

**Decision**

Business logic emits telemetry through `TelemetrySink`, not through direct OpenTelemetry calls.

**Why**

- tests can assert on telemetry without external systems
- different sinks can be swapped by environment
- OTLP remains available without coupling core logic to one vendor API

**Current code anchors**

- `src/unified_modernization/observability/telemetry.py`
- `src/unified_modernization/observability/opentelemetry.py`
- `src/unified_modernization/observability/bootstrap.py`

## ADR-016: Cloud Run for the Initial Gateway Deployment

**Decision**

Use Cloud Run for the pilot deployment of the HTTP gateway and harness.

**Why**

- the current deployable runtime is a stateless HTTP/container surface
- operational overhead is lower than GKE at the current scale
- Cloud Run matches the repo’s actual workload better than a larger orchestration footprint

**Current code anchors**

- `src/unified_modernization/gateway/http_api.py`
- `infra/terraform/run.tf`

## ADR-017: Honest Infrastructure Over Fake Completeness

**Decision**

Provision substrate for the future projection worker, but do not pretend the repo already contains that worker.

**Why**

- fake infrastructure narratives are dangerous in pilot programs
- the repo can honestly deploy the gateway now while preparing the rest of the control plane
- this keeps Terraform aligned with code reality

**Current code anchors**

- `infra/terraform/`
- `docs/TERRAFORM_DEPLOYMENT.md`

## Revisit Triggers

Revisit this ADR set if any of the following change materially:

- the repo adds a real projection-consumer service
- search mode becomes cutover-state-driven instead of env-driven
- publish `409` conflicts are reclassified from DLQ failures to operational no-ops
- Cloud Run stops being the right fit for the gateway deployment shape
- the telemetry protocol needs distributed tracing semantics beyond the current sink abstraction
