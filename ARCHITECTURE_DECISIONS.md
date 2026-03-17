# Unified Modernization Platform — Architecture Decision Record Compendium

> **Canonical ADR set for this repository.**
> **Audience:** principal engineers, staff engineers, and architects.
> **Purpose:** explain every design choice that shapes the implementation that exists today, not an aspirational future platform. A new principal engineer should be able to read this document from start to finish and re-derive why the system has the shape it does, what alternatives were rejected and why, and what consequences each decision carries over time.
>
> **Format per ADR:** Context → Decision → Alternatives Considered → Rationale → Trade-offs → Long-term consequences → Code anchors.

---

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](#adr-001-canonical-event-contract) | Canonical Event Contract | Accepted |
| [ADR-002](#adr-002-fragment-based-projection-assembly) | Fragment-Based Projection Assembly | Accepted |
| [ADR-003](#adr-003-atomic-state-mutation-at-the-store-boundary) | Atomic State Mutation at the Store Boundary | Accepted |
| [ADR-004](#adr-004-native-source-ordering-over-timestamp-ordering) | Native Source Ordering over Timestamp Ordering | Accepted |
| [ADR-005](#adr-005-dual-track-independent-cutover-state-machines) | Dual-Track Independent Cutover State Machines | Accepted |
| [ADR-006](#adr-006-firestore-for-durable-cutover-state) | Firestore for Durable Cutover State | Accepted |
| [ADR-007](#adr-007-external-versioning-for-elasticsearch-writes) | External Versioning for Elasticsearch Writes | Accepted |
| [ADR-008](#adr-008-odata-translation-surface-separate-from-search-routing) | OData Translation Surface Separate from Search Routing | Accepted |
| [ADR-009](#adr-009-application-level-backpressure-with-bypass-conditions) | Application-Level Backpressure with Bypass Conditions | Accepted |
| [ADR-010](#adr-010-application-level-dlq-and-quarantine) | Application-Level DLQ and Quarantine | Accepted |
| [ADR-011](#adr-011-bucketed-anti-entropy-reconciliation) | Bucketed Anti-Entropy Reconciliation | Accepted |
| [ADR-012](#adr-012-per-backend-resilience-wrapper) | Per-Backend Resilience Wrapper | Accepted |
| [ADR-013](#adr-013-shadow-quality-gate-with-judged-relevance) | Shadow Quality Gate with Judged Relevance | Accepted |
| [ADR-014](#adr-014-tenant-aware-routing-and-whale-isolation) | Tenant-Aware Routing and Whale Isolation | Accepted |
| [ADR-015](#adr-015-observability-as-a-protocol) | Observability as a Protocol | Accepted |
| [ADR-016](#adr-016-cloud-run-for-the-initial-gateway-deployment) | Cloud Run for the Initial Gateway Deployment | Accepted |
| [ADR-017](#adr-017-honest-infrastructure-over-fake-completeness) | Honest Infrastructure over Fake Completeness | Accepted |

---

## ADR-001: Canonical Event Contract

**Status:** Accepted

### Context

The platform receives change notifications from five distinct source technologies: Azure SQL (via Debezium CDC envelopes), Cosmos DB (change feed), Google Cloud Spanner (change streams), Google Cloud Firestore (outbox documents), and AlloyDB (also via Debezium). Each source emits records in a proprietary format with different field names, different timestamp representations, different ways of signalling deletes, and different notions of event ordering.

Downstream platform components — the projection engine, the reconciliation engine, the backfill coordinator, the publisher — must process change events regardless of which source produced them.

### Decision

Introduce a single internal canonical event type, `CanonicalDomainEvent`, as the exclusive data contract between source adapters and all downstream platform components. Every source adapter's sole responsibility is to produce this type. Every downstream component accepts only this type.

The canonical contract carries:
- Identity: `domain_name`, `entity_type`, `logical_entity_id`, `tenant_id`
- Provenance: `source_technology`, `source_version`, `event_time_utc`
- Semantics: `change_type` (UPSERT, DELETE, REFRESH, REPAIR), `fragment_owner`
- Payload: opaque `dict` — source adapter strips system fields; remainder is application payload
- Migration context: `migration_stage`
- Observability: `trace_id`
- Computed: `ordering_key` property for Pub/Sub message ordering

### Alternatives Considered

**A1 — Each downstream component handles each source format natively.**
Projection engine, reconciler, and backfill all contain logic to parse Debezium envelopes, Cosmos change feeds, Spanner change streams.
*Rejected:* every new source or schema change propagates across all consumers. The blast radius of a source format change is unbounded. Testing requires constructing source-specific payloads in every test suite.

**A2 — Discriminated union with source-specific branches.**
Pass a tagged union downstream; consumers pattern-match on source type.
*Rejected:* this is canonicalization in disguise without the benefits. Downstream code still couples to source formats. It also complicates cross-domain migration stage tracking, which is source-agnostic.

**A3 — Schema-on-read (raw bytes, lazy parse).**
Pass raw event bytes downstream; consumers parse when they need data.
*Rejected:* defers format errors to processing time rather than ingestion time. A malformed Cosmos record only fails when the projection engine reads it, making root cause analysis harder. Eager validation at the adapter boundary is the correct seam.

### Rationale

A canonical contract is a classical seam. Its value scales with the number of producer–consumer pairs it decouples. With five source technologies and six downstream components, 5 × 6 = 30 potential coupling points are replaced by 5 adapters + 6 consumers = 11 seams.

The `ordering_key` computed property deserves explicit attention. Pub/Sub message ordering requires a stable, deterministic key per logical entity so that all events for the same entity arrive in order at the same consumer. Encoding this into the contract ensures ordering semantics are enforced at the boundary, not scattered across consumer logic.

The `payload` field is intentionally untyped (`dict`). Strongly typing the payload would require the canonical schema to grow with every source domain's field set, creating the exact coupling the contract is meant to prevent. The trade-off is that payload type safety is deferred to the fragment assembly step.

### Trade-offs

**Benefit:** Source format changes require updating one adapter. No downstream changes.

**Cost:** The canonical contract is the most load-bearing schema in the system. Adding fields requires backward compatibility management. Removing fields breaks all adapters simultaneously.

**Cost:** The opaque `payload` dict sacrifices compile-time type checking for payload contents. A badly authored adapter can produce subtly wrong payloads — for example, including `_ts` Cosmos system fields in the payload — without a type error.

### Long-term Consequences

- The canonical contract is permanent and must be treated as a public API. Schema changes need explicit migration planning.
- The `source_version` field must monotonically increase within a logical entity from a given source. Sources that reset sequence counters after failover require adapter-level disambiguation.
- The `migration_stage` field enables routing decisions that are otherwise hard to express — for example, REPAIR events that should bypass backpressure during a fallback window. This field will increase in importance as migration complexity grows.

### Code Anchors

- `src/unified_modernization/contracts/events.py`
- `src/unified_modernization/adapters/`

---

## ADR-002: Fragment-Based Projection Assembly

**Status:** Accepted

### Context

In the operational data model, a single logical entity (for example, a customer document) has its authoritative data spread across multiple source tables or collections: core fields in one Cosmos collection, customer profile enrichment in a second, optional tags from a third service. These arrive as separate change events at different times from different CDC streams, with no guaranteed ordering or co-arrival.

A search document requires all pieces assembled. The system must hold publication until required pieces are present, must not block indefinitely on optional enrichments, and must handle individual fragment updates without reprocessing the entire entity.

### Decision

Model each search document as an assembly of named **fragments**, where each fragment represents the contribution of one named source. Define a `DependencyPolicy` per `(domain_name, entity_type)` that specifies which fragments are required, which are optional, and the freshness TTL for each.

`ProjectionBuilder` maintains a `ProjectionStateRecord` per entity that tracks all received fragments and evaluates completeness against the policy on every incoming event.

Publication decision logic:
1. If any required fragment is absent → `PENDING_REQUIRED_FRAGMENT`
2. If any required fragment exceeds its freshness TTL → `STALE`
3. If all required fragments are present and fresh → `READY_TO_BUILD`
4. Optional fragments beyond their TTL do not block publication (behaviour configurable via `allow_partial_optional_publish`)
5. Only publish if assembled payload has changed since last publication (content-addressed `last_payload_hash`)

### Alternatives Considered

**A1 — Full entity fetch on every event.**
Each incoming event triggers a read of all source tables for that entity.
*Rejected:* at high event rates this creates severe read amplification. It also requires all sources to support efficient point-in-time reads, which CDC-based sources do not.

**A2 — Single-stream projection (one event = one full document).**
Each CDC event carries the full document payload, no assembly needed.
*Rejected:* only works if source tables map 1:1 to complete documents. In practice, multiple sources contribute, and requiring full-document CDC on every partial change is impractical at scale.

**A3 — Streaming join with time windows.**
Use a stream processing engine to windowed-join fragment streams.
*Rejected:* windowed joins require a bounded inter-fragment arrival constraint. In this domain, a `customer_profile` fragment may arrive hours after `document_core` due to an upstream batch job. No viable time window can be set without risking late-arrival loss. The state-based approach that waits indefinitely (with optional TTL escape) is the correct model.

**A4 — Query-time join in Elasticsearch.**
Store raw fragments as nested objects and join at query time.
*Rejected:* Elasticsearch is not a relational store. Query-time joins are expensive, not supported at the required scale, and couple the search schema to the source data model.

### Rationale

The fragment model matches how operational data is actually structured. It avoids both read amplification (full-entity fetch) and late-arrival loss (windowed joins). Storing fragment state durably in Spanner makes the system restart-safe: fragments accumulated before a restart are not lost.

The freshness TTL on required fragments acts as an implicit CDC pipeline heartbeat monitor. If Cosmos DB's change feed silently stalls for eight hours, the `document_core` fragment for every entity eventually ages past its TTL. Documents transition to `STALE`, triggering rehydration work. This surfaces pipeline failures as operational signals rather than allowing stale documents to be published silently.

The content-addressed version check (`last_payload_hash`) prevents no-op publishes from consuming Elasticsearch write capacity on high-frequency touch-but-no-change events — common in scenarios where an upstream batch process re-touches records without actually changing their content.

### Trade-offs

**Benefit:** Multi-source entity assembly without read amplification or bounded arrival constraints.

**Cost:** Misconfigured `DependencyPolicy` silently holds documents in `PENDING_REQUIRED_FRAGMENT` indefinitely. Monitoring of per-domain pending document counts is not optional — it is the primary health signal for this component.

**Cost:** Fragment state in Spanner grows proportionally to entities × fragment owners. For very high-cardinality domains this should be modelled before deployment.

**Cost:** The two-level policy fallback — `(domain_name, entity_type)` then `(None, entity_type)` then `KeyError` — is a runtime error if no matching policy exists. This is intentional fail-closed behaviour, but it means all domain configurations must be complete before events start arriving.

### Long-term Consequences

- Adding a new source fragment to an existing domain requires only a policy update. No code changes. The engine immediately begins collecting the new fragment and incorporating it into completeness evaluation.
- The `QUARANTINED` status is an operator escape hatch for entities stuck due to data quality issues. It must be used with explicit reason logging and should be rare.
- As the domain count grows, projection state table size warrants periodic review. Completed migrations should be archived.

### Code Anchors

- `src/unified_modernization/projection/builder.py`
- `src/unified_modernization/contracts/projection.py`
- `src/unified_modernization/config/loader.py`

---

## ADR-003: Atomic State Mutation at the Store Boundary

**Status:** Accepted

### Context

The projection engine's hot path is: receive event → read current entity state and all fragments → compute mutation → write updated entity state and affected fragment. This read-modify-write sequence must be atomic. Different store backends enforce atomicity differently: Spanner uses read-write transactions with serializable isolation; SQLite uses a file-level lock; an in-memory store uses a Python threading lock.

If the builder owned the transaction boundary, it would need to express the atomic scope differently for each backend. If the transaction boundary were expressed generically by the builder, it would leak Spanner-specific semantics (e.g., `with transaction:` blocks) into the builder.

### Decision

State stores own the transaction boundary via the `mutate_entity(key, mutation_fn)` API. The caller passes a pure function that accepts the current entity state and returns the desired new state. The store executes this function inside its own transaction primitive, regardless of whether that primitive is a Spanner transaction, a SQLite exclusive lock, or a Python threading lock.

The builder never sees a transaction handle. It only sees current state → new state. The store ensures the computation is atomic.

### Alternatives Considered

**A1 — Builder manages transaction boundaries explicitly.**
Builder calls `store.begin()`, reads, computes, writes, `store.commit()`.
*Rejected:* this leaks transaction lifecycle management into the builder, which then couples the builder to each store's transaction model. The builder becomes untestable without a real or simulated transaction engine.

**A2 — Optimistic concurrency with retry (read → write → retry on conflict).**
Builder reads, computes, attempts a conditional write with a version check, retries on conflict.
*Rejected:* valid, but at high event rates retry storms on hot entities become a performance concern. Passing the mutation function into the store eliminates contention by making the read-compute-write atomic without retry.

### Rationale

Inversion of control for mutation is a clean solution to the "who owns atomicity" problem. Each store can use its native transaction mechanism. The builder remains a pure function over state, testable with the in-memory store without mocking. Spanner's `run_in_transaction` lambda-based API maps directly to this pattern.

### Trade-offs

**Benefit:** Builder is transport-agnostic and fully testable in memory. Each store enforces its own concurrency guarantees without leaking them upward.

**Cost:** The mutation function passed to the store must not have side effects that are not idempotent — for example, it must not send network calls or emit irreversible signals inside the transaction. This is a discipline constraint that must be documented and enforced in code review.

**Cost:** The contract between builder and store (`mutate_entity` signature) is the most sensitive internal API in the system. Changes require coordinated updates across all three store implementations.

### Long-term Consequences

- Any future store backend (AlloyDB, CockroachDB) can be added by implementing the Protocol without touching the builder.
- The in-memory store's use of Python threading locks means that concurrent mutations to the same entity serialize on a Python lock, not on a database transaction. Tests that exercise concurrent mutation paths against the in-memory store are testing the right logic, but are not testing Spanner-level isolation semantics. Integration tests against Spanner remain necessary for validating serializable isolation under concurrent writers.

### Code Anchors

- `src/unified_modernization/projection/store.py`

---

## ADR-004: Native Source Ordering over Timestamp Ordering

**Status:** Accepted

### Context

Projection correctness requires that later changes overwrite earlier ones, not the reverse. Determining "later" requires an ordering mechanism. The obvious choice is wall-clock timestamps. The correct choice is native source version numbers.

Sources provide native ordering primitives:
- Debezium CDC: Log Sequence Number (LSN)
- Spanner change streams: `record_sequence`
- Cosmos DB change feed: `_lsn`

These are monotonically increasing within a given source and entity. Wall-clock timestamps from these sources are available but carry risk: same-tick writes (two commits within the same millisecond) produce identical timestamps, and clock skew across database replicas can cause a later write to carry an earlier timestamp.

### Decision

Source adapters extract and use native source version fields as `source_version` in `CanonicalDomainEvent`. Timestamp fallback exists only as an explicit opt-in escape hatch within each adapter, not as a default.

Fragment comparison in the projection builder uses `source_version` to determine whether an incoming fragment supersedes the stored one.

### Alternatives Considered

**A1 — Use event timestamps universally.**
Simpler to understand and consistent across sources.
*Rejected:* timestamps are weaker guarantees. Clock skew between Azure database nodes creates scenarios where a logically later write carries an earlier timestamp. Same-tick writes produce identical timestamps with no resolution. A stale overwrite silently corrupts projection state — there is no error, just wrong data in the search index.

**A2 — Use platform ingestion time (the time the adapter received the event).**
Solves clock skew by using a single clock.
*Rejected:* ingestion time depends on network latency and consumer lag. An event that spent 500ms in transit may be ingested after a later event that was in transit for 10ms. Ingestion ordering is not source ordering.

### Rationale

Native source ordering matches the durability guarantee of the source database. If the database says event B committed after event A (higher LSN), then B's state supersedes A's regardless of the wall-clock timestamp on either. This is the correct semantic for a change data capture system.

Ordering bugs in a search projection engine are silent. A stale overwrite produces a document that is syntactically valid and passes all schema checks but contains data from an earlier point in time. These bugs are discovered by users noticing stale data, which is a terrible failure mode for a financial services platform.

### Trade-offs

**Benefit:** Ordering correctness matches source database guarantees. Stale overwrite bugs are structurally prevented.

**Cost:** Sources that reset their version counter after failover or restore require adapter-level handling. Adapters must document their version reset behaviour and either disambiguate (e.g., by combining LSN with server epoch) or treat the reset as a full rehydration event.

**Cost:** Comparing `source_version` across sources is not meaningful (Cosmos LSN 8000 is not comparable to Spanner sequence 500). The version comparison is scoped to `(entity_id, fragment_owner)` — the same entity from the same source.

### Long-term Consequences

- Any new source adapter must document its native ordering guarantee and its version reset behaviour before being merged.
- If a source is migrated (for example, Cosmos is replaced by Spanner as the source for a domain), the versioning lineage breaks. This must be handled as an explicit reset: a REFRESH event that establishes a new version baseline.

### Code Anchors

- `src/unified_modernization/adapters/debezium_cdc.py`
- `src/unified_modernization/adapters/spanner_change_stream.py`
- `src/unified_modernization/adapters/cosmos_change_feed.py`

---

## ADR-005: Dual-Track Independent Cutover State Machines

**Status:** Accepted

### Context

The migration involves two logically independent operations: moving authoritative data stores from Azure to GCP (backend migration) and moving search serving from Azure AI Search to Elasticsearch (search migration). These have different risk profiles, different validation criteria, different rollback windows, and different business owners. A product team owns search quality; a data engineering team owns data integrity. Their readiness arrives at different times.

### Decision

Model each operation as a separate FSM per domain. Each domain has a `backend_track` FSM and a `search_track` FSM. They share no state and advance independently.

Backend FSM states:
`AZURE_PRIMARY` → `AZURE_PRIMARY_GCP_WARMING` → `GCP_SHADOW_VALIDATED` → `GCP_PRIMARY_FALLBACK_WINDOW` → `GCP_PRIMARY_STABLE`

Search FSM states:
`AZURE_SEARCH_PRIMARY_ELASTIC_DARK` → `AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW` → `ELASTIC_CANARY` → `ELASTIC_PRIMARY_FALLBACK_WINDOW` → `ELASTIC_PRIMARY_STABLE`

State transitions are operator-initiated with required identity and reason fields. Every transition is recorded as an immutable `CutoverTransitionEvent`.

### Alternatives Considered

**A1 — Single unified FSM with combined states.**
One state machine per domain with states like `AZURE_PRIMARY_ELASTIC_DARK`, `GCP_WARMING_ELASTIC_SHADOW`.
*Rejected:* the cartesian product of backend states × search states produces ~25 combined states, most of which are illegal combinations. It also creates artificial coupling — a blocked backend migration prevents search migration from advancing, even though they are independent from a correctness standpoint.

**A2 — Automatic progression when quality gates pass.**
The system advances states without human approval when metrics cross thresholds.
*Rejected:* automatic progression removes human accountability for production state changes. In regulated financial services, a named human with a stated business reason is required before any production step. Automatic progression also creates runaway scenarios where transient metric spikes trigger unintended production state changes.

**A3 — Feature flags.**
`gcp_writes_enabled`, `elastic_search_enabled` booleans per domain.
*Rejected:* feature flags are unordered. They cannot encode the linear progression constraint (elastic search primary should not be enabled before shadow testing passes). They do not capture the fallback window concept, do not produce a human-readable audit trail, and cannot distinguish between "disabled because not yet started" and "disabled because rolled back."

### Rationale

FSMs formalise the concepts of valid state and valid transition — exactly what a zero-downtime migration requires. The formalisation provides: (1) invariant enforcement — the machine rejects illegal state jumps; (2) audit trail — every transition event is immutable; (3) restart safety — rehydration from the event log restores exact position; (4) domain independence — backend and search advance without blocking each other.

Operator-initiated transitions reflect the operational philosophy: quality gates produce evidence; humans make decisions. The platform enforces constraints and surfaces evidence; operators retain authority.

### Trade-offs

**Benefit:** Domain teams operate at their own pace. Independent tracks eliminate artificial blockers.

**Cost:** Two FSMs per domain means operators track twice as many states. A unified migration health dashboard per domain is important for operational clarity.

**Cost:** Operator-initiated transitions create a human-in-the-loop latency. A quality gate may pass at 02:00 but the transition waits until business hours. This is intentional.

### Long-term Consequences

- Adding a third migration track (e.g., an analytics serving migration) requires a new FSM definition and store entry without touching existing tracks.
- The `migration_stage` field on `CanonicalDomainEvent` is derived from FSM state. Components that behave differently at different stages query the FSM. This coupling is acceptable because it is query-only; no component writes FSM state except the cutover module.
- After decommissioning, the Firestore event log answers "when did each domain go live, who approved it, and why?" — a compliance artifact.

### Code Anchors

- `src/unified_modernization/cutover/state_machine.py`

---

## ADR-006: Firestore for Durable Cutover State

**Status:** Accepted

### Context

Cutover state must be durable (survive restarts), append-only (immutable event log), and support transactional latest-state snapshots. Two GCP options were available: Spanner (already used for projection state) and Firestore.

### Decision

Use Google Cloud Firestore (native mode) for cutover state persistence. The store maintains an append-only event log alongside a transactionally updated latest-state snapshot per domain+track.

### Alternatives Considered

**A1 — Reuse Spanner for cutover state.**
Add a `cutover_events` table to the existing Spanner instance.
*Not adopted for initial deployment:* cutover write volume is tiny (5–10 transitions per domain over the lifetime of a migration). Spanner pricing is per processing-unit-hour; charging a dedicated Spanner instance for a handful of writes per day is disproportionate. More importantly, keeping cutover state separate from projection state reduces the blast radius of a Spanner incident — a Spanner failure does not also take down cutover state management.

**A2 — Local JSONL file (for development path).**
Append-only log as a local file; each line is a JSON event.
*Implemented for development but not for production:* the in-process `InMemoryCutoverStateStore` and file-based path are preserved for local and test use. Production requires the Firestore-backed store.

### Rationale

Firestore's document model maps cleanly to the cutover event pattern. The per-domain document collection is the event log; a separate `latest_state` document holds the transactionally updated current state. Firestore's native-mode strong consistency within a region provides the necessary guarantees for latest-state reads without requiring a distributed transaction.

Keeping cutover state in a separate store from projection state aligns with the principle that the two systems have different operational characteristics and failure modes. A production operator should be able to read the current cutover state even during a Spanner incident.

### Trade-offs

**Benefit:** Cost-appropriate for the access pattern; operationally isolated from projection state store.

**Cost:** Two databases to monitor and maintain. Firestore's delete protection must be kept enabled; accidental deletion of the cutover event log would be unrecoverable without a separate backup.

**Cost:** Firestore does not support SQL analytics. Reporting over the migration history (e.g., "how long did each domain spend in each state?") requires scanning documents or exporting to BigQuery.

### Long-term Consequences

- Consider a periodic export of completed migration event logs to BigQuery for analytical reporting. The migration history is a compliance and post-mortem artifact.
- If the platform is extended to manage significantly more domains (hundreds), the current document-per-event model should be reviewed for Firestore read quota implications.

### Code Anchors

- `src/unified_modernization/cutover/state_machine.py`
- `infra/terraform/firestore.tf`

---

## ADR-007: External Versioning for Elasticsearch Writes

**Status:** Accepted

### Context

The Elasticsearch publisher must write documents safely under concurrent projection workers and potential retry scenarios. The two options are: (1) internal versioning, where Elasticsearch manages the document version; (2) external versioning, where the application supplies a monotonically increasing version number and Elasticsearch rejects writes that carry a lower version than the stored one.

### Decision

Use `version_type=external` in Elasticsearch index calls, supplying the platform-owned `projection_version` as the document version. A 409 conflict response (meaning the document in Elasticsearch already has a higher or equal version) is currently treated as a publish failure and routed to the application DLQ for operator review.

### Alternatives Considered

**A1 — Internal versioning only.**
Let Elasticsearch manage the `_version` counter; use optimistic locking with `if_seq_no` / `if_primary_term`.
*Not adopted:* internal versioning requires a read-before-write to obtain the current `_seq_no`. This adds latency and a race window. External versioning allows write-without-read as long as the application-side version is authoritative.

**A2 — Last-writer-wins (no version check).**
Write without any version constraint; most recent write wins by arrival time.
*Rejected:* in a system with multiple concurrent projection workers and retry-on-failure semantics, a retried write of an older version can overwrite a newer write that arrived between the original attempt and the retry. This is a silent data corruption path.

**A3 — Treat 409 as operational no-op (discard without DLQ entry).**
If the stored version is already higher than the incoming version, the document is already correct; discard the conflict silently.
*Not adopted for the initial implementation:* this is logically correct for most 409 scenarios (the stored version is newer), but the DLQ treatment is more operationally conservative. Routing 409s to the DLQ surfaces unexpected conflict patterns for investigation during the initial migration period. This decision is listed as a revisit trigger.

### Rationale

External versioning prevents stale writes from silently overwriting newer documents — the most dangerous failure mode in a search projection system. The `projection_version` counter is owned and incremented by the projection builder only when the assembled payload changes (content-addressed), which makes it a meaningful business-level version rather than an arbitrary sequence.

The conservative DLQ treatment for 409s reflects the pilot stage of the migration. Once the conflict patterns are well understood and confirmed to be benign retries rather than ordering bugs, a future ADR should reclassify 409 handling.

### Trade-offs

**Benefit:** Stale writes are rejected at the Elasticsearch layer, not silently accepted.

**Cost:** 409 conflicts from legitimate retries fill the DLQ and require operator attention. This is intentional during the pilot but should be re-evaluated as the system matures.

**Cost:** External versioning requires the application to be the authoritative version source. If multiple writers increment `projection_version` for the same entity without coordination, conflicts arise. The projection builder's atomic `mutate_entity` call prevents this, but any future parallel writer must use the same coordination mechanism.

### Long-term Consequences

- **Revisit trigger:** once 409 conflict patterns in production are understood, reclassify benign retries as operational no-ops rather than DLQ entries.
- The `projection_version` field in Elasticsearch becomes a durable artifact. Queries can use it to detect documents that are out-of-date relative to the projection store.

### Code Anchors

- `src/unified_modernization/projection/publisher.py`
- `src/unified_modernization/projection/runtime.py`

---

## ADR-008: OData Translation Surface Separate from Search Routing

**Status:** Accepted

### Context

Consumer applications send queries using Azure AI Search OData syntax. Elasticsearch uses a proprietary Query DSL. Migrating search serving transparently requires translating OData to Elasticsearch DSL without requiring consumers to change their query syntax. The question is how to decompose translation from routing.

### Decision

Keep OData translation and routed search orchestration as separate concerns, exposed via two distinct deployed surfaces:

- `asgi.py` — translation-only surface: `/health` and `/translate`. No backend calls, no traffic-mode logic, no shadow or canary behaviour.
- `http_api.py` — deployable HTTP gateway: `/health`, `/translate`, and `/search`. Routes through `SearchGatewayService` with full traffic-mode, shadow, and canary support.

`SearchGatewayService` can be embedded or hosted behind different transports, because translation is not coupled to it.

### Alternatives Considered

**A1 — Combine translation and routing in one surface.**
`/translate` is just an internal utility of `/search`; expose only `/search`.
*Rejected:* removes a standalone debugging and development tool. The `/translate` endpoint is valuable for clients who want to understand the DSL equivalent of their OData query without executing it. Exposing it independently also allows translation testing without routing infrastructure.

**A2 — Require consumers to migrate to Elasticsearch Query DSL directly.**
Remove the OData layer; consumers rewrite their queries.
*Rejected:* coordinating query syntax changes across all consumers and the search backend simultaneously creates a multi-team dependency that serialises the migration. Consumer changes can be made incrementally after the search backend is stable.

### Rationale

Separation allows the translation-only surface to be deployed and tested independently of the full gateway. It also makes the translation logic independently testable, which is important given the semantic risk of OData-to-DSL translation (a translation that is syntactically valid but semantically wrong produces a quality regression that shadow metrics will detect, but a clear unit test is better).

The field mapping dictionary in the translator serves dual purposes: it decouples OData field names from Elasticsearch field names (allowing schema evolution on either side independently) and it blocks a class of injection where a consumer constructs an OData filter referencing internal Elasticsearch fields.

### Trade-offs

**Benefit:** Translation is independently deployable, testable, and debuggable.

**Cost:** Maintaining the OData translation surface is a long-term cost. Once all consumers are on `ELASTIC_ONLY` mode, a future decision is needed: keep OData support for the convenience of existing consumers, or migrate consumers to native Elasticsearch DSL.

**Cost:** Not all OData operators are translated. The supported operator set must be inventoried against actual consumer usage before shadow mode begins.

### Long-term Consequences

- The OData translation layer may become a permanent feature if consumers prefer not to change their query syntax even after the migration completes. This should be an explicit product decision.
- **Revisit trigger:** if the gateway's `SearchGatewayService` is refactored to read traffic mode from Firestore per request (rather than from env vars at startup), the translation-only surface is unaffected — it does not participate in traffic-mode logic.

### Code Anchors

- `src/unified_modernization/gateway/asgi.py`
- `src/unified_modernization/gateway/http_api.py`
- `src/unified_modernization/gateway/service.py`
- `src/unified_modernization/gateway/odata.py`

---

## ADR-009: Application-Level Backpressure with Bypass Conditions

**Status:** Accepted

### Context

The projection pipeline receives events from a Pub/Sub subscription (or directly from adapters during backfill). The arrival rate can exceed the processing and publishing rate during bulk imports, upstream batch jobs, or infrastructure catch-up periods. Without flow control, sustained over-ingestion exhausts memory, causes timeouts, and produces publish failures.

Backpressure must not, however, block the events most needed to resolve the backlog.

### Decision

`ProjectionRuntime` enforces application-level backpressure: when the pending document count for a domain exceeds a configurable threshold (default 100,000), new events are returned with a THROTTLED result code. The caller (streaming consumer or backfill coordinator) is responsible for re-queuing throttled events.

Bypass conditions prevent deadlock:
- Events with `change_type=REPAIR` bypass backpressure (they resolve pending entities, reducing the count)
- Events that complete an in-flight incomplete entity bypass backpressure (same reason)

### Alternatives Considered

**A1 — Broker-level backpressure only.**
Rely on Pub/Sub flow control; pause acking when the pipeline is saturated.
*Insufficient alone:* broker-level flow control is coarse-grained and does not know about per-domain pending counts. The application-level implementation allows domain-specific thresholds and the crucial bypass conditions.

**A2 — Drop events under pressure, with replay from source.**
Under load, discard events and replay from the change log when the backlog clears.
*Rejected:* replay from source is expensive and introduces latency. The re-queue approach keeps events available without source re-read.

**A3 — Uniform backpressure (no bypass).**
All events throttled equally when threshold is exceeded.
*Rejected:* this creates a deadlock scenario. If the backlog is full of entities waiting for a final REPAIR fragment, blocking REPAIR events prevents the backlog from draining. Uniform backpressure can make the threshold self-sustaining.

### Rationale

The 100,000 pending threshold is deliberately generous. At a typical processing rate of 5,000–10,000 documents per second, this represents 10–20 seconds of catch-up work — a temporary spike, not a crisis. The threshold is a signal, not a hard capacity limit.

The bypass conditions reflect the topology of resolution: the events needed to resolve pending state are precisely the events that must not be blocked. Making the bypass explicit (checking `change_type` and pending-entity status) makes the logic auditable.

### Trade-offs

**Benefit:** Bounded memory use under burst; domain-specific thresholds; explicit priority for corrective events.

**Cost:** The THROTTLED result code creates a caller contract. If the streaming consumer discards THROTTLED events instead of re-queuing them, events are silently lost. This contract must be documented and enforced in consumer implementations.

### Long-term Consequences

- As the domain count grows, the threshold should be reviewed per domain rather than as a single global value. High-volume domains may need higher thresholds; low-volume domains where a sudden spike is more anomalous than expected may benefit from tighter thresholds.
- **Revisit trigger:** if the platform adds a dedicated projection-consumer worker process, the backpressure contract should be re-evaluated in the context of that worker's internal queue depth rather than just the pending document count.

### Code Anchors

- `src/unified_modernization/projection/runtime.py`

---

## ADR-010: Application-Level DLQ and Quarantine

**Status:** Accepted

### Context

Individual events can fail to process for multiple reasons: schema errors, transient store unavailability, version conflicts, missing dependency policies, or Elasticsearch publish failures. Failed events must not be silently discarded. They must be captured with enough context to diagnose the failure and, where appropriate, replayed.

### Decision

`ProjectionRuntime` maintains an application-level Dead Letter Queue (DLQ). Events that fail processing after reaching the runtime are written to the DLQ with the original event, the failure timestamp, the error type, and a stack trace. The DLQ is not automatically drained; it requires operator review.

`ProjectionBuilder` supports a `quarantine` operation for entities that are stuck due to data quality issues. A quarantined entity is held in `QUARANTINED` status until an operator explicitly releases it or resolves the underlying issue.

### Alternatives Considered

**A1 — Broker-native DLQ only.**
Rely on Pub/Sub's dead-letter topic for failed messages.
*Insufficient:* the broker-native DLQ captures messages that failed all delivery retries. It does not capture application-level failures that occur after a message is successfully delivered and acked. Projection failures (wrong schema, policy KeyError, publish 409) happen after acking and are invisible to the broker.

**A2 — Log failures and discard.**
Write a log entry and continue processing.
*Rejected:* silent discard creates invisible data gaps. An entity that fails to project silently never appears in the search index. In financial services, this is a correctness failure, not an operational inconvenience.

### Rationale

The DLQ captures failures that occur inside the application processing boundary — after the message has been successfully consumed from the broker. These failures carry richer context than a broker-level dead-letter (they have stack traces and projection state), which is exactly what diagnosis requires.

The `quarantine` operation addresses a distinct failure mode: an entity where the data itself is the problem (malformed source record, policy violation that cannot be auto-resolved). Quarantine prevents endless reprocessing of a known-bad entity while preserving the failure context for later investigation.

### Trade-offs

**Benefit:** No silent event loss; rich failure context; distinction between transient (DLQ-and-replay) and persistent (quarantine) failures.

**Cost:** DLQ depth is an alert condition that requires operator attention. If not monitored, the DLQ becomes a silent sink where failures accumulate without resolution.

**Cost:** Replay from the DLQ requires operators to understand the failure classification and take appropriate action. This is not zero-effort.

### Long-term Consequences

- DLQ replay should be semi-automated in a future version. Currently, replay is a manual operator action. The pattern of replay — re-submit event to main queue after root cause resolution — is well-defined; automating it reduces operational burden.
- The `quarantine_at_utc` timestamp in the projection state record enables queries for "entities quarantined more than N days ago," which supports periodic quarantine review workflows.

### Code Anchors

- `src/unified_modernization/projection/runtime.py`
- `src/unified_modernization/projection/builder.py`

---

## ADR-011: Bucketed Anti-Entropy Reconciliation

**Status:** Accepted

### Context

Before advancing any cutover state, the team must verify that the new system holds exactly the same data as the old system: no missing documents, no extras, no content drift. At the scale of production financial services data (potentially tens of millions of documents per domain), comparing documents one-by-one is infeasible from a time and compute perspective.

### Decision

Implement a bucketed hash-first reconciliation: hash all documents into 1,024 buckets by document ID, compute a single aggregate checksum per bucket, compare bucket checksums between old and new systems, and drill into individual documents only within buckets whose checksums differ.

Mismatch categories reported: missing, unexpected, checksum drift, delete-state mismatch, tenant-scope mismatch, cohort-scope mismatch.

A `remote_store seam` enables paginated bucket fetches against live stores without loading the full corpus into memory.

### Alternatives Considered

**A1 — Full document-by-document scan.**
Load all documents from both systems, sort by key, compare line-by-line.
*Rejected:* infeasible at scale. Loading 50 million documents from two systems into memory simultaneously is not viable. Even streaming comparison is O(N) regardless of mismatch rate — 99.9% correct migrations waste enormous compute on the 99.9% that matches.

**A2 — Count-only reconciliation.**
Compare total document counts; equal counts = correct data.
*Rejected:* count equality does not imply content equality. A deletion plus an insertion cancel out in a count comparison. Count check is necessary but not sufficient; it is used as a fast preliminary filter before bucket comparison.

**A3 — Consistent hashing with virtual nodes.**
Use consistent hashing for bucket assignment.
*Not adopted:* consistent hashing is designed for distributed systems where nodes join and leave dynamically. Reconciliation is a batch process. Deterministic modulo hashing (`SHA-256(doc_id) % 1024`) is simpler and sufficient.

### Rationale

The bucketed approach reduces reconciliation work proportionally to the correctness of the migration. A migration that is 99.9% correct examines 0.1% of buckets in detail. A migration that is 100% correct traverses all bucket checksums (fast) and exits without examining any individual documents.

The remote_store seam is essential for production use against large stores. Without it, reconciliation requires loading the entire corpus into the host process's memory — infeasible for large domains.

### Trade-offs

**Benefit:** Reconciliation cost scales with the number of mismatches, not total document count.

**Cost:** The 1,024 bucket count is a fixed parameter. Too few buckets and each bucket is large, increasing work when a bucket disagrees. Too many and the bucket checksum comparison phase becomes the bottleneck. 1,024 is a reasonable default for millions of documents; domains with very different cardinalities should evaluate this parameter.

**Cost:** SHA-256 checksum per document adds computational overhead for domains with high write rates. This is a reconciliation-time cost, not a hot-path cost.

### Long-term Consequences

- Reconciliation should run continuously during the migration (daily or weekly) to detect drift early, not only as a pre-cutover gate.
- The bucket count should be treated as a deployment-time parameter with documented selection criteria, not as an arbitrary constant. Changing it mid-migration requires re-running a full reconciliation from scratch.
- The tenant-scope and cohort-scope mismatch categories anticipate the multi-tenant, multi-cohort reality of the data model. As new organizational groupings are introduced, the mismatch category list may need extension.

### Code Anchors

- `src/unified_modernization/reconciliation/engine.py`

---

## ADR-012: Per-Backend Resilience Wrapper

**Status:** Accepted

### Context

The search gateway calls two external systems: Azure AI Search and Elasticsearch. Both can exhibit transient failures (timeouts, 5xx responses), and can enter extended degradation that requires circuit-breaking. The requirement is that shadow backend failures must never affect primary backend correctness, and that any latency impact from the shadow path be kept inside an explicit timeout-and-retry budget.

### Decision

Wrap each search backend in `ResilientSearchBackend`, which composes three resilience patterns:

1. **Timeout:** each backend call is bounded; calls exceeding the timeout are aborted and treated as failures
2. **Bounded linear retry backoff:** transient failures are retried up to N times with `retry_backoff_seconds * attempt`
3. **Circuit breaker:** consecutive failures above a threshold open the circuit; calls are rejected immediately; after a recovery timeout, a probe call is attempted; success closes the circuit

The wrapper is applied independently to each backend. Shadow backend failures are best-effort in the sense that they do not change which backend's result is returned as the primary answer. However, in the current `SearchGatewayService` implementation, sampled shadow calls are still awaited on the request path, so shadow degradation can increase end-user latency even when primary correctness remains intact.

### Alternatives Considered

**A1 — Per-client retry logic inside each backend client.**
Each backend client class implements its own retry and timeout.
*Rejected:* duplicates resilience logic across two clients. Circuit breaker state must be scoped to the backend across all retries, which requires it to live outside the per-call client logic.

**A2 — Service mesh (Istio, Envoy) for resilience.**
Delegate retry, timeout, and circuit breaking to a sidecar.
*Rejected:* Cloud Run does not support sidecar injection in standard configuration. Service mesh adds operational complexity disproportionate to the current scale.

**A3 — Naive fixed retry without circuit breaker.**
Three fixed retries with a fixed sleep between each.
*Rejected:* without circuit breaking, every incoming request attempts retries against a degraded backend, consuming thread pool slots and adding latency to every query for the duration of the degradation. This is the thundering herd anti-pattern.

### Rationale

Timeout + retry + circuit breaker is the standard resilience composition for external HTTP dependencies. Each pattern addresses a distinct failure class: timeout prevents threads from blocking indefinitely; retry recovers from transient errors; circuit breaker prevents cascade during extended degradation. Together they provide isolation: a failing backend degrades gracefully rather than bringing down the gateway.

The current implementation intentionally keeps retry behavior simple: `await asyncio.sleep(retry_backoff_seconds * attempt)`. That is linear backoff without jitter. This is acceptable for the pilot-scale gateway, but it should be treated as an implementation detail, not as a resilience guarantee. If synchronized retry storms appear under load, adding jitter is the first follow-up hardening step.

### Trade-offs

**Benefit:** Backend failures are contained. Primary correctness remains isolated from shadow-backend failures.

**Cost:** Primary latency is not fully isolated from sampled shadow work in the current implementation. Shadow failures eventually fall back to the primary result, but only after the shadow timeout/retry budget is consumed.

**Cost:** Circuit breaker state is per process instance (not shared across Cloud Run instances). A backend that is failing may be seen as healthy by some instances and failed by others. This means circuit breaker signals must be aggregated across instances in the monitoring dashboard to produce a coherent view.

**Cost:** Retry logic can mask systematic errors. If Elasticsearch consistently fails due to a query translation error, retries exhaust before surfacing the error clearly. Circuit breaker metrics must be monitored for patterns.

### Long-term Consequences

- As request volume grows and Cloud Run scales to many instances, per-instance circuit breaker state becomes less useful as a protection mechanism (instances that see the failure open their circuit, but others continue). Consider externalising circuit breaker state to Firestore or Redis if this proves problematic at scale.
- If shadow-mode latency becomes operationally significant, decouple shadow execution from the synchronous request path before increasing `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT`.
- Timeout values should be derived from measured p99 latency plus a buffer, not from guesswork. Instrument both backends in shadow mode before finalising timeout values.
- **Revisit trigger:** if the Cloud Run deployment is replaced by GKE with Istio, resilience can be delegated to the service mesh. Application-level wrappers would then be redundant.

### Code Anchors

- `src/unified_modernization/gateway/resilience.py`
- `src/unified_modernization/gateway/bootstrap.py`

---

## ADR-013: Shadow Quality Gate with Judged Relevance

**Status:** Accepted

### Context

Moving search serving to Elasticsearch carries the risk of search quality regression. This risk cannot be fully evaluated in a staging environment because query distributions and relevance patterns in production differ from test data. Quality assessment must happen under real production conditions before customer-visible traffic is shifted.

### Decision

Implement a shadow and canary quality gate with two evaluation paths:

- **Always-available live comparison:** overlap rate at K, computed from the IDs returned by both backends (minimum 0.5 when no judged metrics are available)
- **Optional judged relevance metrics:** when a `QueryJudgmentProvider` supplies a relevance judgment, compute `shadow_ndcg_at_10`, `shadow_mrr`, and `ndcg_drop = primary_ndcg_at_10 - shadow_ndcg_at_10`

Gate logic:
- If judged metrics are present, fail when `shadow_ndcg_at_10 < 0.85`, or `ndcg_drop > 0.10`, or `shadow_mrr < 0.5`
- If judged metrics are absent, fall back to overlap-rate gating and fail when overlap rate at K is below `0.5`

Gate breach during canary mode triggers `canary_auto_disabled`, sets `canary_frozen = true`, and sets in-process `canary_percent = 0`. Primary canary routing stops immediately, although sampled shadow comparisons may still continue while the service remains in CANARY mode.

Shadow observations are sampled (not 100% of traffic) via `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` to control load on Elasticsearch before it is production-sized.

An optional `QueryJudgmentProvider` interface accepts externally labelled relevance judgements for offline-corpus-backed NDCG computation.

### Alternatives Considered

**A1 — Manual quality review (human spot-check).**
Export sample queries, compare manually, approve when the team is satisfied.
*Rejected:* not scalable to the tail of query patterns. Subjective. Cannot detect gradual regressions during the canary period.

**A2 — A/B test with downstream business metrics.**
Split traffic, measure click-through rates or conversion as quality signals.
*Rejected:* business metrics are lagging indicators. A search regression surfaces in customer satisfaction scores days or weeks later. By then, the regression has affected real customers at scale.

**A3 — Offline evaluation only.**
Build a labelled query corpus, evaluate both systems offline before the migration.
*Insufficient alone:* offline evaluation cannot cover the full distribution of production queries, and labelled corpora decay as data evolves. Offline evaluation using `QueryJudgmentProvider` is a complement, not a substitute.

### Rationale

Shadow mode provides unlimited observation time with zero correctness exposure. Every sampled query processed in shadow mode contributes evidence about Elasticsearch quality without changing which backend supplies the primary answer. In the current implementation, however, sampled shadow work is still on the request path, so shadow observation is not zero-latency-cost for the caller.

NDCG@10 is the industry-standard metric for search relevance evaluation. It measures not only whether the right results appear in the top 10, but whether they appear in the right order, with higher-ranked results weighted more heavily. This is the correct metric for evaluating ranking quality, not just recall.

Automatic canary freeze is a safety net that operates without human monitoring. A latent regression that only manifests at higher traffic levels (e.g., a tail query pattern that becomes common at 25% canary) triggers protection automatically before reaching 50%.

### Trade-offs

**Benefit:** Quality regressions are caught before customers see them. Automatic freeze provides safety without 24/7 monitoring.

**Cost:** At 100% observation, shadow mode roughly doubles Elasticsearch query volume during validation. At lower observation percentages, the additional query load scales with `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT`. Elasticsearch must still be sized for the chosen observation rate.

**Cost:** Overlap rate is a lagging indicator of index divergence. Low overlap can mean ranking difference (a quality problem) or index completeness gap (a pipeline problem). Operators must distinguish these causes before acting.

**Cost:** Quality gate thresholds set too tight produce false alarms; too loose allow regressions through. The defaults are conservative; they should be tuned based on actual query distribution characteristics.

### Long-term Consequences

- The `QueryJudgmentProvider` interface is an extension point for relevance annotation tools. Investing in a labelled query corpus significantly improves the precision of quality evaluation.
- Shadow/canary infrastructure should be preserved after migration completes. It provides a safe path for future Elasticsearch configuration changes (index rebuilds, mapping updates, query tuning) without customer exposure.
- **Revisit trigger:** if `canary_auto_disabled` is a frequent false-positive (gateway restarts reset it but underlying quality is fine), consider persisting the frozen state to Firestore so it survives restarts.

### Code Anchors

- `src/unified_modernization/gateway/evaluation.py`
- `src/unified_modernization/gateway/service.py`

---

## ADR-014: Tenant-Aware Routing and Whale Isolation

**Status:** Accepted

### Context

The platform is multi-tenant. Tenants vary dramatically in document volume. Large "whale" tenants can account for 30% or more of total document volume. The search index design must support per-tenant query isolation, tenant-specific operational management (reindex one tenant without affecting others), and prevent whale tenants from degrading shared infrastructure.

Routing decisions must be stable across service restarts — a document indexed to a given alias must remain findable via the same alias after a restart.

### Decision

`TenantPolicyEngine` implements two routing strategies:

**Dedicated routing (whale tenants):** tenants listed in `UMP_DEDICATED_TENANTS` receive exclusive index aliases: `{domain}-{tenant_id}-{read|write}`.

**Shared routing (all others):** tenants are hash-bucketed into two shared groups (SHARED_A or SHARED_B) using `SHA-256(tenant_id) % 2`. Write alias: `{domain}-shared-{group}-write`. The hash is stable across restarts and deployments.

`IngestionPartitionPolicyEngine` manages write-side partitioning separately from query routing: whale tenants get dedicated ingestion lanes; shared tenants distribute across 32 partitions via `SHA-256(tenant_id) % 32`.

### Alternatives Considered

**A1 — Single shared index for all tenants.**
All tenants in one index, filtered at query time by `tenant_id`.
*Rejected:* a whale tenant's document volume dominates shard distribution, creating hot shards. Corpus-wide relevance scoring is distorted by the whale tenant's distribution, affecting ranking for all others. Operational management (reindex one tenant) is impossible without affecting all tenants.

**A2 — One index per tenant.**
Each tenant has its own index regardless of size.
*Rejected:* Elasticsearch has documented stability limits on shard count per node (approximately 1,000 shards). With hundreds of tenants and a 1 primary + 1 replica minimum, this limit is easily exceeded.

**A3 — Random routing with metadata.**
Route randomly at ingest time; use routing queries at search time.
*Rejected:* random routing breaks the determinism requirement. After a restart, tenant A's documents might be routed differently, causing query misses. Stable hash guarantees are essential for index consistency.

### Rationale

Stable hashing provides determinism without an external routing table. SHA-256 is collision-resistant, uniformly distributed, and produces consistent results across any Python version or platform. The modulo operation is simple and auditable.

The dedicated tenant list provides an operator escape valve for whale tenants without algorithmic changes. Adding a tenant to the list requires only a configuration update and a targeted reindex.

### Trade-offs

**Benefit:** Consistent, collision-resistant routing with no external lookup. Whale tenant isolation prevents noisy-neighbour effects.

**Cost:** With two shared buckets, imbalanced tenant size distributions create hot buckets. Monitor bucket document counts. If imbalance is significant, increase to four or more buckets.

**Cost:** Moving a tenant from shared to dedicated requires reindexing that tenant's documents. During reindex, a brief window exists where documents may be in both the old shared index and the new dedicated index. Read alias routing must be updated before the reindex completes.

### Long-term Consequences

- The `% 2` bucket count for shared tenants is a deployment-time parameter that appears in the code as an implementation detail. Changing it mid-migration requires reindexing all shared tenant documents. Document this clearly.
- The ingestion partition count (32) and the search routing bucket count (2) are independent. They serve different purposes and should not be conflated.
- Consider automating tenant promotion to dedicated status based on document count thresholds, rather than requiring manual configuration updates.

### Code Anchors

- `src/unified_modernization/routing/tenant_policy.py`

---

## ADR-015: Observability as a Protocol

**Status:** Accepted

### Context

The platform emits telemetry at every significant decision point: projection completeness decisions, shadow quality comparisons, circuit breaker state changes, cutover transitions. This telemetry must be: (1) testable without external infrastructure, (2) cheap in development, (3) pluggable in production (different deployments use different observability backends — Datadog, Grafana, GCP Cloud Monitoring).

### Decision

Define `TelemetrySink` as a Python Protocol with three methods: `emit` (structured event), `increment` (counter), `record_timing` (latency histogram). Provide four implementations selectable by `UMP_TELEMETRY_MODE`:

- `noop` — discards all events (CI pipelines)
- `memory` — collects in-process (unit and integration tests)
- `logger` — writes structured JSON to stdout (non-production)
- `otlp_http` — ships to an OpenTelemetry HTTP collector (production)

An `OpenTelemetryBridge` maps the platform's internal telemetry model to OTLP-compatible spans and metrics.

### Alternatives Considered

**A1 — Direct OpenTelemetry SDK calls throughout the codebase.**
Instrument directly with the OTel Python SDK.
*Rejected:* every significant code path calls OTel directly. Changing the observability backend (or adding a test spy) requires either mocking the OTel SDK or replacing it entirely. This creates tight coupling between production logic and the observability vendor API.

**A2 — Python logging module only.**
Emit all telemetry as Python log records.
*Rejected:* log records are string-oriented; metrics (counters, histograms) are not naturally expressible as log records. Structured event semantics (trace IDs, typed attributes) require workarounds.

### Rationale

The Protocol approach decouples instrumentation from export. Every instrumented code path calls `sink.emit(...)` — a trivial, infrastructure-free call. In tests, the `MemoryTelemetrySink` allows assertions like "exactly one `search.shadow.regression` event was emitted with these attributes," providing test coverage for observability behaviour itself.

In production, the OTLP bridge translates the platform's internal event model to OpenTelemetry spans and metrics without requiring the application code to know the difference.

### Trade-offs

**Benefit:** Observability behaviour is testable. Backend changes require only a sink swap at bootstrap time.

**Cost:** The Protocol interface is a lowest-common-denominator API. Advanced OpenTelemetry features (baggage propagation, span links, exemplars) require extending the interface or using the OTLP bridge directly for those specific call sites.

### Long-term Consequences

- The `trace_id` field on all telemetry events enables distributed trace correlation across the platform. Ensure trace IDs from incoming requests are propagated consistently through all internal operations.
- As the platform grows, the `MemoryTelemetrySink` test pattern becomes a first-class contract. Tests that assert on telemetry are implicitly asserting on observable behaviour; treat them as a form of integration test, not as optional coverage.

### Code Anchors

- `src/unified_modernization/observability/telemetry.py`
- `src/unified_modernization/observability/opentelemetry.py`
- `src/unified_modernization/observability/bootstrap.py`

---

## ADR-016: Cloud Run for the Initial Gateway Deployment

**Status:** Accepted

### Context

The search gateway must be deployed to GCP with auto-scaling, health checks, and managed TLS. The primary options are Cloud Run (serverless container) and Google Kubernetes Engine (managed Kubernetes).

### Decision

Deploy the search gateway as a Cloud Run service for the pilot deployment. The harness load-testing job is deployed as a Cloud Run Job.

### Alternatives Considered

**A1 — Google Kubernetes Engine (GKE).**
Full Kubernetes cluster with Horizontal Pod Autoscaler.
*Not adopted for initial deployment:* GKE adds significant operational overhead (node pool management, cluster upgrades, networking, RBAC, PodDisruptionBudgets) that is disproportionate for a single-service, stateless workload. The team's capacity is better spent on migration logic than on Kubernetes operations at this stage.

**A2 — Cloud Run for Anthos.**
Cloud Run with Kubernetes backing.
*Not adopted:* adds Anthos licensing and operational complexity without material benefit at current scale.

### Rationale

The deployed search gateway is stateless and horizontally scalable, which is exactly the workload Cloud Run optimises for. Platform durability lives elsewhere in the system (Spanner for projection state, Firestore for cutover state), but the gateway itself starts from environment configuration plus Secret Manager-backed credentials. Auto-scaling from zero eliminates idle cost. The Terraform default `gateway_allow_unauthenticated = false` prevents unauthenticated public access at the platform edge.

Fail-closed production startup is implemented in application bootstrap, not through an explicit Cloud Run readiness or startup probe. In production environments, invalid gateway or telemetry bootstrap raises during app construction and the process fails to start, so the revision never becomes healthy enough to serve traffic.

`min_instances = 1` should be set in production to eliminate cold-start latency on the hot path.

### Trade-offs

**Benefit:** Zero cluster management. Auto-scaling responds within seconds. Misconfigured production deployments fail closed before receiving traffic because startup raises before the service can begin handling requests.

**Cost:** Cloud Run cold starts (~1–3 seconds for the Python runtime) affect tail latency under bursty traffic. Setting `min_instances = 1` eliminates this on the primary path but retains it for scale-out instances.

**Cost:** Cloud Run has a maximum request timeout (configurable, up to 3,600 seconds). Very long-running operations (reconciliation, bulk backfill) must be implemented as Cloud Run Jobs, not as requests to the gateway service.

### Long-term Consequences

- **Revisit trigger:** if the platform grows to include multiple services (projection worker, reconciliation worker, backfill coordinator), GKE becomes more appropriate than managing many independent Cloud Run services. Evaluate when the service count exceeds three to four.
- Gateway and harness Cloud Run service accounts currently follow least-privilege for the assets they actually use: they receive access only to the specific Secret Manager secrets required by their runtime. The separately provisioned projection-runtime service account carries the broader Pub/Sub, Spanner, and Datastore roles needed for a future worker. Do not widen any of these permissions without explicit justification.

### Code Anchors

- `src/unified_modernization/gateway/http_api.py`
- `infra/terraform/run.tf`

---

## ADR-017: Honest Infrastructure over Fake Completeness

**Status:** Accepted

### Context

The platform's Terraform stack provisions Pub/Sub topics, subscriptions, and supporting infrastructure for a future projection-consumer worker process. However, no such worker process exists in the repository today. The Terraform could provision substrate that implies a complete, deployed system; or it could provision only what is actually deployed.

### Decision

Provision the Pub/Sub substrate (topics, subscriptions, IAM) as infrastructure ready for a future worker, but do not implement a fake worker service, placeholder Cloud Run service, or documentation that implies the worker exists. The repo documents explicitly what it does and does not ship.

### Rationale

Fake infrastructure narratives are dangerous in pilot programs and migrations. If the documentation or Terraform implies that a projection-consumer worker is running in production and it is not, an operator who relies on that assumption will make incorrect decisions — for example, assuming that events are being continuously consumed from Pub/Sub and reaching Spanner.

The principle of honest infrastructure: provision real substrate; document gaps explicitly. This makes the system's actual capabilities and limitations clear to everyone who operates it.

### Trade-offs

**Benefit:** No false confidence about system completeness. Operators know exactly what is running.

**Cost:** The provisioned Pub/Sub substrate without a consumer means topics will accumulate unacked messages if any producer starts writing to them before the consumer is implemented. Monitor Pub/Sub topic age and message count; set appropriate retention and ack deadlines.

### Long-term Consequences

- When the projection-consumer worker is implemented, the Pub/Sub substrate is already provisioned and IAM-bound. Deployment requires only wiring the worker to the existing infrastructure.
- The `docs/IMPLEMENTATION_ROADMAP.md` documents the intended consumer implementation as a future phase. This document and `DEVELOPER_GUIDE.md` are the canonical references for what is still missing.

### Code Anchors

- `infra/terraform/`
- `docs/TERRAFORM_DEPLOYMENT.md`

---

## Revisit Triggers

Revisit specific ADRs if any of the following change materially:

| Trigger | Affects |
|---|---|
| A dedicated projection-consumer worker process is added to the repo | ADR-009, ADR-017 |
| Search mode becomes Firestore-driven (read per request) rather than env-driven at startup | ADR-008 |
| Elasticsearch 409 conflicts are reclassified from DLQ failures to operational no-ops | ADR-007 |
| Cloud Run stops being the right deployment model (service count grows, GKE is adopted) | ADR-016, ADR-012 |
| Telemetry protocol needs distributed tracing semantics beyond the current sink abstraction | ADR-015 |
| Canary freeze state must survive service restarts (currently per-process) | ADR-013 |
| Shared tenant bucket count needs to increase beyond 2 | ADR-014 |
| A new source technology is onboarded with atypical ordering semantics | ADR-004 |
