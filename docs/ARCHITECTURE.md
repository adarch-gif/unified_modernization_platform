# Architecture

## Design goal

This repository implements the reusable backbone for a unified modernization initiative:

- one canonical event plane
- one projection plane
- one Search Gateway
- one reconciliation model
- one tenant-routing policy model

But:

- backend-primary cutover stays independent
- search-serving cutover stays independent
- domain target-store decisions stay flexible

## Main components

### Canonical event contract

All source adapters normalize vendor-specific changes into `CanonicalDomainEvent`. The projection layer only depends on those canonical events.

### Projection builder

The projection builder solves the incomplete-projection problem by:

- storing fragment state per logical entity
- resolving dependency policy by domain and entity type, not entity type alone
- applying dependency rules
- refusing to publish when required fragments are missing or stale
- incrementing a projection version only when the materialized document changes
- mutating one logical entity under one store-controlled boundary instead of a multi-call read/modify/write sequence

The builder now depends on a `ProjectionStateStore` contract. The repo includes:

- `InMemoryProjectionStateStore` for fast local tests
- `SqliteProjectionStateStore` as a durable local control-plane implementation
- `SpannerProjectionStateStore` as the production-oriented control-plane implementation path, including explicit Spanner DDL for fragments and projection state

The store contract now supports entity-level mutation. That means:

- the builder hands one logical entity mutation to the store
- the store owns atomicity and revision updates
- local runs use a lock-backed in-memory store
- durable runs use SQLite or Spanner transaction boundaries

The production path is to implement the same contract on Spanner or an equivalently durable control-plane store.

The runtime bootstrap now makes this a deployment invariant:

- local/dev/test may use the in-memory store
- staging/production must provide an explicit durable store
- the helper in `projection/bootstrap.py` exists so service startup cannot silently default to in-memory state

### Backfill coordinator

Historical migration must not overload the real-time event path. The backfill coordinator therefore models:

- bulk side-load directly into the projection plane
- explicit capture of source high watermarks
- typed watermark formats so different source technologies are distinguishable at handoff
- resumable checkpoints for long-running bulk loads
- a deterministic handoff to streaming CDC for the delta gap
- prioritization of rehydration or repair for entities left pending after side-load

### Search Gateway

The gateway preserves a stable client-facing contract and supports:

- Azure-only mode
- shadow mode
- canary mode
- Elastic-only mode

The gateway is tenant-routed. Queries resolve through the tenant policy engine into shared or dedicated Elasticsearch aliases before being sent to the target backend.

The gateway also now supports:

- operational shadow quality gates
- telemetry-ready events when ranking quality falls below threshold
- automatic canary freeze when judged regression exceeds configured thresholds
- best-effort shadow execution so primary-serving traffic survives shadow-only failures
- resilient backend wrappers for timeout, retry, and circuit-breaker behavior
- concrete Azure AI Search and Elasticsearch query backends that fit the gateway `SearchBackend` protocol
- a smoke/load harness that replays representative query cases through the exact gateway runtime path
- bootstrap-time enforcement so production startup does not silently use raw backends or no-op telemetry

### Search evaluation

Search cutover evidence must include more than overlap counts. The evaluation harness now supports:

- live shadow overlap metrics
- offline judged relevance metrics such as `NDCG@10` and `MRR`
- zero-result-rate tracking for query corpora

### Cutover state machines

Backend and search state machines are separate because unified platform does not mean big-bang cutover.

The cutover path is now restart-safe:

- transitions are persisted as append-only events
- current domain state is rehydrated from the persisted log on startup
- operators can supply reason and actor metadata per transition
- deployed environments can back the cutover log with Firestore instead of local JSONL durability

There is now a cutover bootstrap seam mirroring the projection and gateway bootstraps:

- local/dev/test may use in-memory cutover state for fast tests
- staging/production must provide an explicit durable cutover store
- the helper in `cutover/bootstrap.py` exists so migration orchestration cannot silently default to ephemeral state

### Reconciliation

The reconciliation engine now compares:

- total counts
- missing and unexpected documents
- checksum drift
- tenant-scope mismatches
- cohort-scope mismatches
- delete-state mismatches

The repo also includes a bucketed anti-entropy layer that:

- hashes documents into deterministic buckets
- compares bucket digests before comparing individual documents
- recursively refines only mismatched buckets when detailed document fingerprints are available
- supports a remote-store seam so mismatched buckets can be paged in over the network instead of materializing full snapshots

This is intentionally Merkle-like rather than a naive full-snapshot compare. The remote-store seam exists so deployed implementations can back drill-down with bounded digest and paginated document APIs instead of whole-dataset transfer.

## Operational wrappers

The repo now includes operational scaffolding around the core architecture:

- `projection/runtime.py` adds backpressure control, priority-aware bypass for completion traffic, and dead-letter handling around projection mutation
- `projection/publisher.py` adds an Elasticsearch publisher with external versioning, tenant-aware alias routing, and bulk publish support
- `routing/tenant_policy.py` now includes a dedicated ingestion partition policy for whale tenants
- `observability/telemetry.py` adds structured events, counters, timings, and trace-like spans
- `observability/opentelemetry.py` bridges the telemetry protocol into OpenTelemetry-compatible traces and metrics
- `observability/bootstrap.py` adds environment-driven telemetry sink selection for local harnesses and deployed services
- `gateway/bootstrap.py` makes resiliency and telemetry explicit deployment requirements instead of conventions
- `gateway/asgi.py` adds API-key protection, bounded request parsing, and explicit `422` translation failures for the exposed gateway endpoints
- `config/loader.py` removes handwritten per-domain Python policy construction
- `adapters/` now includes concrete Firestore outbox, Cosmos change feed, Debezium-style CDC, and Spanner change-stream normalizers

## Production evolution path

The code in this repo is still a starter, but it now includes the main scale seams plus concrete query and CDC integrations. The next production step is wiring the Spanner store, Firestore cutover store, concrete search clients, and publisher to real deployed environments, then filling in any remaining domain-specific CDC envelopes or source-side enrichments.
