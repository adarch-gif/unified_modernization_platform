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
- applying dependency rules
- refusing to publish when required fragments are missing or stale
- incrementing a projection version only when the materialized document changes

The builder now depends on a `ProjectionStateStore` contract. The repo includes:

- `InMemoryProjectionStateStore` for fast local tests
- `SqliteProjectionStateStore` as a durable local control-plane implementation
- `SpannerProjectionStateStore` as the production-oriented control-plane implementation path, including explicit Spanner DDL for fragments and projection state

The production path is to implement the same contract on Spanner or an equivalently durable control-plane store.

### Backfill coordinator

Historical migration must not overload the real-time event path. The backfill coordinator therefore models:

- bulk side-load directly into the projection plane
- explicit capture of source high watermarks
- a deterministic handoff to streaming CDC for the delta gap
- prioritization of rehydration or repair for entities left pending after side-load

### Search Gateway

The gateway preserves a stable client-facing contract and supports:

- Azure-only mode
- shadow mode
- canary mode
- Elastic-only mode

The gateway is tenant-routed. Queries resolve through the tenant policy engine into shared or dedicated Elasticsearch aliases before being sent to the target backend.

### Search evaluation

Search cutover evidence must include more than overlap counts. The evaluation harness now supports:

- live shadow overlap metrics
- offline judged relevance metrics such as `NDCG@10` and `MRR`
- zero-result-rate tracking for query corpora

### Cutover state machines

Backend and search state machines are separate because unified platform does not mean big-bang cutover.

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
- drills into only mismatched buckets when detailed document fingerprints are available

The next production extension should add multi-level bucket recursion or Merkle-tree style validation for very large domains.

## Production evolution path

The code in this repo is still a starter, but it now includes the main scale seams. The next production step is wiring the Spanner store to a real deployed database, then wiring real adapters for Azure SQL, Cosmos, Spanner, Firestore outbox, and AlloyDB CDC.
