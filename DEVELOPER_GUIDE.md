# Unified Modernization Platform — Developer Guide

> **Audience:** Engineers onboarding to this codebase for the first time.
> **Goal:** After reading this document you will understand what the platform does, why each module exists, how data flows end-to-end, how to run it locally, and how to extend it safely.

---

## Table of Contents

1. [What This Platform Does](#1-what-this-platform-does)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Repository Layout](#3-repository-layout)
4. [Core Data Models](#4-core-data-models)
5. [Module Reference](#5-module-reference)
   - 5.1 [Adapters — CDC Normalization](#51-adapters--cdc-normalization)
   - 5.2 [Contracts — Canonical Types](#52-contracts--canonical-types)
   - 5.3 [Config — Domain Onboarding](#53-config--domain-onboarding)
   - 5.4 [Projection — Fragment Assembly](#54-projection--fragment-assembly)
   - 5.5 [Cutover — Migration State Machine](#55-cutover--migration-state-machine)
   - 5.6 [Gateway — Search Traffic Management](#56-gateway--search-traffic-management)
   - 5.7 [Routing — Tenant Policy Engine](#57-routing--tenant-policy-engine)
   - 5.8 [Reconciliation — Anti-Entropy Engine](#58-reconciliation--anti-entropy-engine)
   - 5.9 [Backfill — Bulk Side-Load Coordinator](#59-backfill--bulk-side-load-coordinator)
   - 5.10 [Observability — Telemetry Abstractions](#510-observability--telemetry-abstractions)
6. [End-to-End Data Flow: Event Ingestion Path](#6-end-to-end-data-flow-event-ingestion-path)
7. [End-to-End Data Flow: Search Query Path](#7-end-to-end-data-flow-search-query-path)
8. [Cutover State Machine Walk-Through](#8-cutover-state-machine-walk-through)
9. [Projection Completeness Logic — Step by Step](#9-projection-completeness-logic--step-by-step)
10. [Tenant Routing Deep Dive](#10-tenant-routing-deep-dive)
11. [Configuration Reference](#11-configuration-reference)
12. [Local Development Setup](#12-local-development-setup)
13. [Running the Test Suite](#13-running-the-test-suite)
14. [Environment Variables Reference](#14-environment-variables-reference)
15. [Production Deployment Checklist](#15-production-deployment-checklist)
16. [Extending the Platform](#16-extending-the-platform)
17. [Failure Modes & Operational Runbooks](#17-failure-modes--operational-runbooks)
18. [Glossary](#18-glossary)

---

## 1. What This Platform Does

The Unified Modernization Platform is a **zero-downtime migration framework** that moves a financial-services workload from **Azure (SQL + Azure AI Search)** to **GCP (Cloud Spanner + Firestore)** while simultaneously rerouting search traffic from **Azure AI Search** to **Elasticsearch**.

The two migrations are **independent tracks** that can be advanced at different speeds per domain:

| Track | Current State | Target State |
|---|---|---|
| **Backend** (operational data) | Azure SQL / Cosmos DB | GCP Spanner / Firestore |
| **Search** (query serving) | Azure AI Search | Elasticsearch |

The platform achieves this through five capabilities:

1. **CDC Normalization** — ingests change events from any source (Debezium/Azure SQL, Cosmos change feed, Spanner change stream, Firestore outbox) and converts them into a single `CanonicalDomainEvent` format.
2. **Fragment-Based Projection** — assembles multi-source entity snapshots from fragments, enforcing freshness and completeness rules before publishing to a search index.
3. **Dual-Track Cutover FSM** — a persisted finite-state machine that controls which backends are authoritative and which search index serves live traffic.
4. **Search Gateway** — a reverse proxy that routes, shadows, canaries, and quality-gates traffic between Azure AI Search and Elasticsearch without any client-side changes.
5. **Anti-Entropy Reconciliation** — a bucketed Merkle-tree engine that detects drift between source and target stores and surfaces actionable findings.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          CHANGE DATA SOURCES                            │
│  Debezium/Azure SQL  │  Cosmos Change Feed  │  Spanner Change Stream   │
│                      │  Firestore Outbox    │  AlloyDB (future)         │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ raw vendor events
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          ADAPTER LAYER                                  │
│  DebeziumChangeEventAdapter  │  CosmosChangeFeedAdapter                │
│  SpannerChangeStreamAdapter  │  normalize_outbox_record()              │
│                                                                         │
│  Output: CanonicalDomainEvent (unified event envelope)                 │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ CanonicalDomainEvent
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       PROJECTION RUNTIME                                │
│                                                                         │
│  ProjectionRuntime                                                      │
│  ├── BackpressureController  (throttle when pending > threshold)        │
│  ├── ProjectionBuilder       (fragment assembly + completeness check)   │
│  │   └── ProjectionStateStore (InMemory / SQLite / Spanner)            │
│  ├── ElasticsearchDocumentPublisher  (external versioning, bulk)        │
│  └── DeadLetterQueue         (failed events quarantined here)          │
│                                                                         │
│  Output: SearchDocument published to Elasticsearch index               │
└──────────────────────────────────────────────────────────────┬──────────┘
                                                               │ published documents
                                                               ▼
                                                    ┌──────────────────┐
                                                    │  Elasticsearch   │
                                                    │  (target index)  │
                                                    └────────┬─────────┘
                                                             │
┌─────────────────────────────────────────────────┐          │
┌─────────────────────────────────────────────────┐
│        ASGI TRANSLATION SURFACE (asgi.py)       │
│                                                 │
│  build_app(ASGIRuntimeConfig) → Starlette       │
│  ├── APIKeyAndBodyLimitMiddleware               │
│  │   ├── GET /health → 200 OK (always)         │
│  │   ├── Bad/missing API key → 401             │
│  │   └── Body > max_body_bytes (64KB) → 413   │
│  └── POST /translate                           │
│      ODataTranslator (OData → ES DSL only)     │
│                                                 │
│  Env: GATEWAY_ENVIRONMENT  GATEWAY_API_KEYS    │
│       GATEWAY_MAX_BODY_BYTES  GATEWAY_FIELD_MAP │
└────────────────────┬────────────────────────────┘
                     │ (deployed separately)
┌────────────────────▼────────────────────────────┐
│       SEARCH GATEWAY SERVICE (service.py)       │        ┌──────────────┐
│                                                 │        │ Azure AI     │
│  SearchGatewayService                          │◄──────►│ Search       │
│  ├── TrafficMode: AZURE_ONLY / SHADOW /         │        └──────────────┘
│  │               CANARY / ELASTIC_ONLY          │
│  ├── ResilientSearchBackend (retry+circuit)     │
│  ├── ShadowQualityGate (NDCG@10, MRR)          │
│  └── TenantPolicyEngine (alias routing)        │
│                                                 │
│  Env: UMP_GATEWAY_MODE  UMP_GATEWAY_*           │
│       UMP_AZURE_SEARCH_*  UMP_ELASTICSEARCH_*  │
└─────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│              CUTOVER STATE MACHINE                           │
│                                                              │
│  DomainMigrationState per domain                            │
│  ├── Backend Track FSM  (5 states, forward + one rollback)  │
│  └── Search Track FSM   (5 states, forward + rollback)      │
│                                                              │
│  Store options: InMemory | JsonFile | Firestore (prod)      │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│              RECONCILIATION ENGINE                           │
│                                                              │
│  BucketedReconciliationEngine                               │
│  ├── Build bucketed Merkle-digest snapshots of both stores  │
│  ├── Compare aggregate checksums (O(bucket_count) first)    │
│  └── Drill-down into mismatching buckets (O(documents))     │
│                                                              │
│  Output: ReconciliationReport with severity-tagged findings │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Layout

```
unified-modernization-platform/
│
├── pyproject.toml                  # Build, deps, mypy, ruff, pytest config
├── README.md                       # One-page intro
├── DEVELOPER_GUIDE.md              # ← you are here
│
├── examples/
│   ├── domain_config.yaml          # Domain onboarding YAML example
│   ├── gateway_runtime.env.template # Gateway env-var template
│   └── search_harness_cases.jsonl  # Load-test case examples
│
├── scripts/
│   └── run_gateway_harness.ps1     # PowerShell harness launcher
│
├── src/
│   └── unified_modernization/
│       ├── adapters/               # CDC source normalization
│       │   ├── base.py             # SourceAdapter Protocol
│       │   ├── cosmos_change_feed.py
│       │   ├── debezium_cdc.py
│       │   ├── firestore_outbox.py
│       │   └── spanner_change_stream.py
│       │
│       ├── backfill/
│       │   └── coordinator.py      # Bulk side-load + handoff planning
│       │
│       ├── config/
│       │   └── loader.py           # YAML domain config loader
│       │
│       ├── contracts/
│       │   ├── events.py           # CanonicalDomainEvent, ChangeType, MigrationStage
│       │   └── projection.py       # Fragment, State, SearchDocument models
│       │
│       ├── cutover/
│       │   ├── bootstrap.py        # Factory with env safety checks
│       │   └── state_machine.py    # FSM + store implementations
│       │
│       ├── gateway/
│       │   ├── asgi.py             # ASGI app + auth middleware
│       │   ├── bootstrap.py        # HTTP gateway factory + env loading
│       │   ├── clients.py          # Azure AI Search + Elasticsearch HTTP clients
│       │   ├── evaluation.py       # NDCG@10, MRR, shadow quality gate
│       │   ├── harness.py          # Load-test harness + smoke test CLI
│       │   ├── odata.py            # OData → Elasticsearch DSL translator
│       │   ├── resilience.py       # Circuit breaker + retry + timeout wrapper
│       │   └── service.py          # SearchGatewayService (traffic orchestration)
│       │
│       ├── observability/
│       │   ├── bootstrap.py        # TelemetrySink factory + env loading
│       │   ├── opentelemetry.py    # OTLP-compatible sink implementation
│       │   └── telemetry.py        # TelemetrySink Protocol + all sink impls
│       │
│       ├── projection/
│       │   ├── bootstrap.py        # Builder + publisher factories
│       │   ├── builder.py          # Fragment assembly + publication logic
│       │   ├── publisher.py        # Elasticsearch document publisher (sync+async)
│       │   ├── runtime.py          # Runtime wrapper (backpressure + DLQ)
│       │   └── store.py            # State stores (InMemory / SQLite / Spanner)
│       │
│       ├── reconciliation/
│       │   └── engine.py           # Flat + bucketed reconciliation engines
│       │
│       └── routing/
│           └── tenant_policy.py    # Tenant routing + ingestion partitioning
│
└── tests/
    ├── conftest.py
    ├── test_asgi.py                # ASGI auth + routing tests
    ├── test_backfill.py
    ├── test_config.py
    ├── test_cutover.py
    ├── test_deployment_harness.py
    ├── test_gateway.py             # Gateway service + OData + circuit breaker
    ├── test_integrations.py        # End-to-end integration tests (23 scenarios)
    ├── test_observability.py
    ├── test_projection.py
    ├── test_projection_runtime.py
    ├── test_reconciliation.py
    └── test_routing.py
```

---

## 4. Core Data Models

Understanding these four models is the prerequisite to understanding everything else.

### 4.1 `CanonicalDomainEvent` (`contracts/events.py`)

The **single event envelope** that every adapter outputs. All downstream components (projection, runtime, publisher) consume only this type — never raw vendor records.

```python
class CanonicalDomainEvent(BaseModel):
    event_id: str                     # UUID, auto-generated
    domain_name: str                  # e.g. "customer_documents"
    entity_type: str                  # e.g. "customerDocument"
    logical_entity_id: str            # Business key (not DB row ID)
    tenant_id: str                    # Multi-tenant discriminator
    source_technology: SourceTechnology  # azure_sql | cosmos | spanner | ...
    source_version: int               # LSN or timestamp (nanoseconds) — monotonic
    event_time_utc: datetime          # When the change occurred in the source
    change_type: ChangeType           # upsert | delete | refresh | repair
    fragment_owner: str               # Which data source owns this fragment
    payload: dict[str, Any]           # The actual changed fields
    migration_stage: MigrationStage   # Current migration phase
    trace_id: str                     # Distributed trace correlation ID
```

**Key design decision — `ordering_key`:**
The property `ordering_key = f"{tenant_id}|{domain_name}|{logical_entity_id}"` must be used as the message-broker partition/ordering key so all change events for a given entity arrive in causal order on the same consumer lane.

**`source_version` semantics:**
This integer must be monotonically increasing per `(tenant_id, entity_type, logical_entity_id, fragment_owner)`. The projection store uses it to guard against out-of-order replay — a newer fragment replaces an older one, never the reverse.

### 4.2 `FragmentRecord` and `ProjectionEntityRecord` (`contracts/projection.py`)

An entity is assembled from **multiple fragments** — each fragment comes from a different `fragment_owner` (e.g., `document_core` from Azure SQL, `customer_profile` from Cosmos).

```python
class FragmentRecord(BaseModel):
    fragment_owner: str        # Which source produced this fragment
    source_version: int        # LSN/timestamp of this specific change
    payload: dict[str, Any]    # Fields contributed by this source
    delete_flag: bool          # True if this source signals deletion

class ProjectionEntityRecord(BaseModel):
    key: ProjectionKey                     # (tenant, domain, entity_type, id)
    fragments: dict[str, FragmentRecord]   # keyed by fragment_owner
    state: ProjectionStateRecord | None    # Current pipeline state
    revision: int                          # Store-level optimistic lock version
```

### 4.3 `ProjectionStateRecord` (`contracts/projection.py`)

Tracks where an entity is in its journey through the pipeline.

```python
class ProjectionStateRecord(BaseModel):
    status: ProjectionStatus               # See lifecycle below
    reason_code: str                       # Human-readable code for current status
    projection_version: int                # Increments on every publish
    missing_required_fragments: list[str]  # Fragment owners not yet seen
    stale_required_fragments: list[str]    # Fragments past their freshness TTL
    completeness_status: CompletenessStatus  # complete | partial | deleted
    last_payload_hash: str | None          # SHA-256 of last published payload
    entity_revision: int                   # Monotonic revision counter
    quarantine_reason: str | None          # Reason if quarantined
```

**Entity lifecycle states:**

```
PENDING_REQUIRED_FRAGMENT ──(missing fragment arrives)──► READY_TO_BUILD
PENDING_REHYDRATION       ──(stale fragment refreshed)──► READY_TO_BUILD
READY_TO_BUILD            ──(mutate_entity called)     ──► PUBLISHED
PUBLISHED                 ──(all fragments deleted)    ──► DELETED
PUBLISHED                 ──(fragment update, same hash)► PUBLISHED (no-op)
any state                 ──(poison message)           ──► QUARANTINED
```

### 4.4 `SearchDocument` (`contracts/projection.py`)

The normalized document pushed to Elasticsearch after projection completes.

```python
class SearchDocument(BaseModel):
    document_id: str                    # = logical_entity_id
    tenant_id: str
    domain_name: str
    entity_type: str
    projection_version: int             # For external versioning in ES
    completeness_status: CompletenessStatus
    source_versions: dict[str, int]     # {fragment_owner: source_version}
    payload: dict[str, Any]             # Merged payload from all fragments
```

---

## 5. Module Reference

### 5.1 Adapters — CDC Normalization

**Purpose:** Convert raw vendor-specific change records into `CanonicalDomainEvent`.
**Location:** `src/unified_modernization/adapters/`

All adapters implement the `SourceAdapter` protocol:

```python
class SourceAdapter(Protocol):
    def normalize(self, record: Mapping[str, object]) -> CanonicalDomainEvent: ...
```

#### Debezium CDC Adapter (`debezium_cdc.py`)

Used for **Azure SQL** and **AlloyDB** sources via the Debezium Kafka connector.

**Input record shape (Debezium envelope):**
```json
{
  "payload": {
    "op": "u",
    "before": { ... },
    "after": { "id": "123", "tenant_id": "t1", "lsn": "0/1AF3C0", ... },
    "source": { "lsn": "0/1AF3C0", "ts_ms": 1700000000000 },
    "ts_ms": 1700000000000
  }
}
```

**Operation codes:** `c`/`u`/`r` → `UPSERT`, `d` → `DELETE`, `t` → raises (TRUNCATE must be handled upstream).

**LSN parsing — critical detail:**
SQL Server and PostgreSQL both use hex-encoded Log Sequence Numbers (LSNs) as monotonic version stamps. The adapter supports four formats:

| Format | Example value | Parsed as |
|---|---|---|
| `DECIMAL` | `"123456789"` | `int("123456789")` |
| `POSTGRES_LSN` | `"0/1AF3C0"` | `(0x0 << 32) | 0x1AF3C0` |
| `SQLSERVER_LSN` | `"00000028:00000B80:0001"` | `(0x28 << 48) | (0xB80 << 16) | 0x1` |
| `AUTO` | any of the above | Try Postgres `/` then SQL Server `:` then decimal |

The integer encoding preserves LSN ordering as a simple `>=` comparison.

**Timestamp fallback:**
When `source_version_field` is unavailable, the adapter can fall back to `ts_ns` → `ts_us × 1000` → `ts_ms × 1_000_000` (all normalized to nanoseconds). This is opt-in via `allow_timestamp_source_version_fallback=True` because timestamps are not as reliable as native LSNs. A one-time warning is emitted (thread-safe via `threading.Event`).

**Configuration:**
```python
DebeziumChangeEventAdapterConfig(
    domain_name="customer_documents",
    entity_type="customerDocument",
    fragment_owner="document_core",
    source_technology=SourceTechnology.AZURE_SQL,
    tenant_field="tenant_id",
    logical_entity_id_field="id",
    source_version_field="lsn",
    lsn_format=DebeziumLSNFormat.AUTO,
    allow_timestamp_source_version_fallback=False,
    migration_stage=MigrationStage.AZURE_PRIMARY,
)
```

#### Cosmos Change Feed Adapter (`cosmos_change_feed.py`)

Used for **Azure Cosmos DB** sources. The Cosmos change feed emits full documents.

**Default field names (Cosmos-specific — note camelCase for `tenantId`):**

| Config field | Default value | Notes |
|---|---|---|
| `tenant_field` | `"tenantId"` | **camelCase** — differs from other adapters that default to `"tenant_id"` |
| `logical_entity_id_field` | `"id"` | Standard Cosmos document ID |
| `source_version_field` | `"_lsn"` | Cosmos internal LSN field |
| `event_time_field` | `"_ts"` | Cosmos UNIX timestamp field (int seconds) |
| `change_type_field` | `"operationType"` | Application-level change type field |

**Key behaviors:**
- All fields starting with `_` are stripped from the payload (Cosmos system fields: `_rid`, `_self`, `_etag`, `_attachments`, `_ts`, etc.), plus any additional fields listed in `excluded_fields`.
- `_parse_datetime()` handles multiple types: native `datetime`, `int/float` (UNIX timestamp), `str` (ISO 8601 with `Z`→`+00:00` conversion).
- `_parse_int()` handles `int`, `float` (if `.is_integer()`), and `str` values for `source_version`.
- `change_type` mapping: `"delete"/"deleted"/"remove"` → `DELETE`; `"repair"/"refresh"` → the corresponding enum; everything else → `UPSERT`.
- `source_technology` is always `SourceTechnology.COSMOS` (not configurable per-instance).

#### Spanner Change Stream Adapter (`spanner_change_stream.py`)

Used for **GCP Cloud Spanner** sources when Spanner itself becomes a primary.

**Input:** Spanner DataChangeRecord format with `mod_type`, `mods` (array of key→new_values), `commit_timestamp`, `record_sequence`.

**Key behaviors:**
- `record_sequence` is parsed as source_version. The parser tries direct integer conversion first, falls back to digit extraction with a warning log (covers non-standard Spanner formats). An `_INT64_MAX` overflow guard prevents out-of-range values.
- **`migration_stage` default is `MigrationStage.GCP_PRIMARY_STABLE`** (not `AZURE_PRIMARY` as in other adapters). This is intentional — the Spanner adapter is only active when Spanner has already become the primary backend.
- DELETE events use `old_values` from the mod; if `old_values` is empty, falls back to `keys`.

**Configuration note:** Do not set `migration_stage=AZURE_PRIMARY` on a Spanner adapter — that combination is logically inconsistent (Spanner is a GCP service).

#### Firestore Outbox Adapter (`firestore_outbox.py`)

A simplified adapter for services that publish to Firestore using the outbox pattern. The `FirestoreOutboxRecord` Pydantic model maps directly to `CanonicalDomainEvent` via `normalize_outbox_record()`.

---

### 5.2 Contracts — Canonical Types

**Location:** `src/unified_modernization/contracts/`

These are pure data models — no logic except validators. They are the contracts between every layer:

| File | Key types |
|---|---|
| `events.py` | `CanonicalDomainEvent`, `ChangeType`, `SourceTechnology`, `MigrationStage` |
| `projection.py` | `FragmentRecord`, `ProjectionEntityRecord`, `ProjectionStateRecord`, `SearchDocument`, `PublicationDecision`, `DependencyPolicy`, `DependencyRule` |

**`DependencyPolicy` — fragment rules:**

```python
DependencyPolicy(
    domain_name="customer_documents",   # None = matches all domains for this entity_type
    entity_type="customerDocument",
    rules=[
        DependencyRule(owner="document_core",    required=True, freshness_ttl_seconds=900),
        DependencyRule(owner="customer_profile", required=True, freshness_ttl_seconds=900),
        DependencyRule(owner="optional_tags",    required=False, freshness_ttl_seconds=3600),
    ],
    allow_partial_optional_publish=False,
)
```

A `DependencyRule` with `required=True` and `freshness_ttl_seconds=900` means: this fragment **must** exist AND must have arrived within the last 15 minutes. If it's older, the entity goes to `PENDING_REHYDRATION`.

---

### 5.3 Config — Domain Onboarding

**Location:** `src/unified_modernization/config/loader.py`

Domains are onboarded via YAML files, not code changes:

```yaml
# examples/domain_config.yaml
domains:
  - name: customer_documents
    entity_type: customerDocument
    current_source: cosmos         # azure_sql | cosmos | spanner | firestore
    target_store: spanner          # spanner | elasticsearch | bigquery | ...
    routing:
      dedicated_tenants:
        - tenant-enterprise-001
    projection_policy:
      allow_partial_optional_publish: false
      rules:
        - owner: document_core
          required: true
          freshness_ttl_seconds: 900
        - owner: customer_profile
          required: true
          freshness_ttl_seconds: 900
        - owner: optional_tags
          required: false
          freshness_ttl_seconds: 3600
```

Load in code:
```python
from unified_modernization.config.loader import load_domain_configs, load_dependency_policies

configs = load_domain_configs("path/to/domain_config.yaml")
policies = load_dependency_policies("path/to/domain_config.yaml")
```

---

### 5.4 Projection — Fragment Assembly

**Location:** `src/unified_modernization/projection/`

This is the core engine. Four components work together:

#### `ProjectionBuilder` (`builder.py`)

The stateless logic layer. It receives a `CanonicalDomainEvent`, loads/creates the entity, applies the fragment, checks completeness, and returns a `PublicationDecision`.

**Critical method: `upsert(event, now_utc) -> PublicationDecision`**

See section 9 for the step-by-step algorithm.

**Key invariant — LSN guard:**
```python
if current is None or incoming.source_version >= current.source_version:
    entity.fragments[incoming.fragment_owner] = incoming
```
A fragment is accepted only if its `source_version` is at least as new as the stored fragment. This prevents out-of-order replay from corrupting state.

#### `ProjectionStateStore` (`store.py`)

Three implementations, all behind the same Protocol:

| Implementation | Use case | Durability |
|---|---|---|
| `InMemoryProjectionStateStore` | Unit tests, local dev | Process lifetime |
| `SqliteProjectionStateStore` | Local integration tests, pilot | File-backed, WAL mode |
| `SpannerProjectionStateStore` | Production | Cloud Spanner, interleaved tables |

**The `mutate_entity()` contract:**
```python
def mutate_entity(
    self,
    key: ProjectionKey,
    mutator: Callable[[ProjectionEntityRecord], ProjectionMutationResult],
) -> ProjectionMutationResult: ...
```
This is an atomic read-modify-write. The store fetches the entity, calls the mutator (which contains all the business logic), and writes the result atomically. Callers never hold references to mutable store state between calls.

**Spanner table layout** — retrieve via `SpannerProjectionStateStore.projection_schema_ddl()`:
```sql
CREATE TABLE projection_states (
  tenant_id STRING(MAX) NOT NULL,
  domain_name STRING(MAX) NOT NULL,
  entity_type STRING(MAX) NOT NULL,
  logical_entity_id STRING(MAX) NOT NULL,
  status STRING(MAX) NOT NULL,
  payload_json STRING(MAX) NOT NULL,
  commit_timestamp TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id);

CREATE TABLE projection_fragments (
  tenant_id STRING(MAX) NOT NULL,
  domain_name STRING(MAX) NOT NULL,
  entity_type STRING(MAX) NOT NULL,
  logical_entity_id STRING(MAX) NOT NULL,
  fragment_owner STRING(MAX) NOT NULL,
  source_version INT64 NOT NULL,
  payload_json STRING(MAX) NOT NULL,
  commit_timestamp TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner),
  INTERLEAVE IN PARENT projection_states ON DELETE CASCADE;
```

**`commit_timestamp`** uses `allow_commit_timestamp=true` so Spanner fills it in atomically at commit time — never set it manually.

**`entity_revision`** (optimistic lock counter) is incremented on every `_finalize_mutation()` call. Spanner `mutate_entity()` raises `ValueError("projection entity mutation must persist a state record")` if the mutator returns a `None` state — callers must always return a state record.

#### `ProjectionRuntime` (`runtime.py`)

The operational wrapper that adds:
- **Backpressure:** If `pending_count >= max_pending_documents` (default: **100,000**), events are throttled unless a bypass condition applies.
  - **Bypass condition 1 — Priority change type:** `REPAIR` or `REFRESH` events always bypass, tagged `reason="priority_change_type"`.
  - **Bypass condition 2 — Pending entity completion:** If the entity's current status is `PENDING_REQUIRED_FRAGMENT`, `PENDING_REHYDRATION`, `STALE`, `QUARANTINED`, or `READY_TO_BUILD`, the event bypasses backpressure, tagged `reason="pending_entity_completion"`. This ensures entities already in the pipeline can complete even under load.
  - Events that fail both bypass checks are throttled (`result.throttled=True`); caller must retry or send to a retry queue.
- **Dead Letter Queue:** If projection fails (exception in `upsert()`), the entity is quarantined AND the event is written to DLQ. If publish fails, the event+document are written to DLQ.
- **Telemetry counters:** `projection.published`, `projection.pending`, `projection.failed`, `projection.publish_failed`, `projection.backpressure.rejected`, `projection.backpressure.bypassed`, `projection.quarantine_failed`.
- **Async support:** `process_async()` wraps all sync store calls in `asyncio.to_thread()` to avoid blocking the event loop. The `_publish_document_async()` helper detects whether the publisher has a native `publish_async()` method; if not, it also wraps `publish()` in `asyncio.to_thread()`.

#### `ElasticsearchDocumentPublisher` (`publisher.py`)

Publishes `SearchDocument` to Elasticsearch using **external versioning**:

```
PUT /{alias}/_doc/{document_id}?version={projection_version}&version_type=external&routing={tenant_id}
```

This ensures that a re-delivered older version of a document never overwrites a newer one already in the index. Deletes use `DELETE /{alias}/_doc/{id}`.

**Key implementation details:**
- `trace_id` is sent as `X-Opaque-Id` header — this appears in Elasticsearch slow logs and task API, enabling end-to-end trace correlation into the cluster.
- The default serializer (`_default_document_body`) merges the payload with platform metadata and adds a `_meta` sub-object:
  ```python
  body["_meta"] = {
      "document_id": ..., "tenant_id": ..., "domain_name": ...,
      "entity_type": ..., "projection_version": ...,
      "completeness_status": ..., "source_versions": ...,
  }
  ```
  A custom `serializer: Callable[[SearchDocument], dict[str, Any]]` can be injected at construction time to override this.
- **Sync/async guard:** Calling `publish()` from an async context (running event loop) raises `RuntimeError` immediately. Always use `publish_async()` in async code.
- **Bulk API:** `publish_many(documents)` / `publish_many_async(documents)` send a single `POST /_bulk` request with NDJSON. Per-item errors are extracted and returned in `result["failed_items"]` — the call does not raise on partial failure.
- Telemetry: `projection.publisher.indexed`, `projection.publisher.deleted`, `projection.publisher.bulk_requests`, `projection.publisher.bulk_failures`.

---

### 5.5 Cutover — Migration State Machine

**Location:** `src/unified_modernization/cutover/`

Each domain has a `DomainMigrationState` instance with two independent FSMs:

#### Backend Track

Controls which operational data store is authoritative:

```
AZURE_PRIMARY
    │ advance
    ▼
AZURE_PRIMARY_GCP_WARMING      ← GCP is warming up, Azure still primary
    │ advance
    ▼
GCP_SHADOW_VALIDATED           ← GCP data validated against Azure
    │ advance
    ▼
GCP_PRIMARY_FALLBACK_WINDOW    ← GCP is primary, Azure on standby
    │ advance          │ rollback
    ▼                  ▼
GCP_PRIMARY_STABLE    AZURE_PRIMARY
```

#### Search Track

Controls which search index serves live traffic:

```
AZURE_SEARCH_PRIMARY_ELASTIC_DARK      ← Elastic not receiving traffic
    │ advance
    ▼
AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW    ← Elastic receives shadow traffic
    │ advance
    ▼
ELASTIC_CANARY                         ← % of traffic to Elastic
    │ advance                 │ rollback
    ▼                         ▼
ELASTIC_PRIMARY_FALLBACK_WINDOW    AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW
    │ advance                 │ rollback
    ▼                         ▼
ELASTIC_PRIMARY_STABLE    AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW
```

**Transition enforcement:**
The `_BACKEND_TRANSITIONS` and `_SEARCH_TRANSITIONS` dicts declare the only valid transitions. `DomainMigrationState.transition_backend(target)` raises `ValueError` for illegal jumps.

**State persistence:**

| Store | Use case |
|---|---|
| `InMemoryCutoverStateStore` | Tests, local dev |
| `JsonFileCutoverStateStore` | Pilot / single-node durability |
| `FirestoreCutoverStateStore` | Production — transactional Firestore with `run_transaction` |

The Firestore store writes both the event log (`__events` collection) and the latest snapshot (`__state/latest` document) in a **single transaction**, preventing TOCTOU race conditions when two operators advance the FSM concurrently.

**Usage:**
```python
from unified_modernization.cutover.state_machine import (
    DomainMigrationState, BackendPrimaryState, SearchServingState,
    FirestoreCutoverStateStore
)

state = DomainMigrationState(
    domain_name="customer_documents",
    store=FirestoreCutoverStateStore(firestore_client),
)
state.transition_backend(
    BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING,
    operator="alice@corp.com",
    reason="GCP Spanner instance validated"
)
```

---

### 5.6 Gateway — Search Traffic Management

**Location:** `src/unified_modernization/gateway/`

The gateway subsystem has **two distinct components** that must not be confused:

1. **ASGI Translation Surface** (`asgi.py`) — a Starlette ASGI app that translates OData queries to Elasticsearch DSL. It has no knowledge of backend routing, traffic modes, or shadow evaluation. It is a standalone process deployed as an ASGI endpoint.
2. **`SearchGatewayService`** (`service.py` + `bootstrap.py`) — the backend-routing orchestrator that implements traffic modes (SHADOW, CANARY, etc.), circuit breaking, and shadow quality evaluation.

These two components are **independently instantiated**. `build_app()` does not accept a `SearchGatewayService` — the ASGI app only translates queries.

#### ASGI Translation Surface (`asgi.py`)

```
Request
  └── APIKeyAndBodyLimitMiddleware
      ├── GET /health → 200 OK (always, no auth required)
      ├── Bad/missing API key (non-dev environments) → 401 UNAUTHORIZED
      ├── Content-Length > max_body_bytes (default 64 KB) → 413
      └── POST /translate → ODataTranslator
          Converts OData query params → Elasticsearch DSL JSON
          Returns: {"query": {...}, "size": N, "from": N, "sort": [...], "aggs": {...}}
```

**Security note:** The middleware is **fail-closed** — if `valid_api_keys` is empty or the key is not in the set, the request is rejected with **401** (not 403). This prevents a misconfiguration from opening an unauthenticated endpoint in production.

**Environment variables (no `UMP_` prefix — ASGI-specific):**

| Variable | Description | Default |
|---|---|---|
| `GATEWAY_ENVIRONMENT` | Environment name (`dev`/`staging`/`prod`); `dev` skips auth | `dev` |
| `GATEWAY_API_KEYS` | Comma-separated list of valid API keys | — |
| `GATEWAY_MAX_BODY_BYTES` | Request body size limit in bytes | `65536` (64 KB) |
| `GATEWAY_FIELD_MAP` | `AzureField=esField,...` OData→ES field renames | — |

**Deployment:** a module-level `app = build_app()` instance is created for ASGI server (uvicorn/gunicorn) pickup:
```python
# gunicorn / uvicorn entry point
uvicorn unified_modernization.gateway.asgi:app --host 0.0.0.0 --port 8080
```

#### Search Gateway Service (`service.py` + `bootstrap.py`)

`SearchGatewayService` is instantiated via `build_http_search_gateway_service_from_env()`, which reads `UMP_GATEWAY_*` env vars (see §14 for the full map). It owns all backend HTTP clients, the circuit breaker layer, and the traffic-mode logic.

#### OData Translator (`odata.py`)

Converts OData query parameters to Elasticsearch DSL:

| OData | Elasticsearch DSL |
|---|---|
| `$search=gold customer` | `multi_match: {query: "gold customer"}` |
| `$filter=Status eq 'ACTIVE'` | `term: {Status: "ACTIVE"}` |
| `$filter=Tier gt 3` | `range: {Tier: {gt: 3}}` |
| `$filter=search.in(Status,'ACTIVE,PENDING')` | `terms: {Status: ["ACTIVE","PENDING"]}` |
| `$orderby=CreatedDate desc` | `sort: [{CreatedDate: {order: "desc"}}]` |
| `$top=10&$skip=20` | `size: 10, from: 20` |
| `$facets=Status,Tier` | `aggs: {Status: {terms:...}, Tier: {terms:...}}` |

Compound boolean expressions with parentheses are supported. Field mapping (`Status → status`) is configurable.

#### Traffic Modes

`SearchGatewayService` supports four modes, controlled by `TrafficMode`:

**Note on Azure auth header:** Azure AI Search uses the `api-key` header (not `Authorization`). Elasticsearch uses `Authorization: ApiKey <key>`. This matters when constructing client configs manually — the wrong header returns 401.

**`AZURE_ONLY`:** All requests go to Azure AI Search. Elasticsearch is dark.

**`SHADOW`:**
1. Query Azure (primary) — response returned to client immediately
2. Query Elastic (shadow) — best-effort, errors suppressed
3. Compare results (overlap rate, identical order)
4. Evaluate quality gate (NDCG@10, MRR if judgment available)
5. Record telemetry

**`CANARY`:**
- Deterministic bucketing: `MD5(tenant_id:consumer_id) % 100 < canary_percent` → Elastic
- Canary bucket gets Elastic response; Azure queried as shadow
- Non-canary bucket gets Azure response; Elastic queried as shadow
- **Auto-disable:** If shadow quality gate fires a regression, `canary_frozen=True` and `canary_percent=0` are set atomically via `threading.RLock()`. All traffic falls back to Azure until manually re-enabled.

**`ELASTIC_ONLY`:** All requests go to Elasticsearch. Azure is dark.

**Sampling:** `shadow_observation_percent` controls what fraction of requests actually perform the shadow comparison (default **25%** in `GatewayRuntimeConfig`). Reducing this limits load on the shadow backend during high-traffic periods.

#### Backend HTTP Clients (`clients.py`)

`AzureAISearchBackend` and `ElasticsearchSearchBackend` are async httpx clients that normalize the respective APIs into a shared response shape: `{results: [...], count: N, facets: {...}, raw: {...}}`.

**`AzureAISearchBackend`:**
- Issues `POST /indexes/{index}/docs/search?api-version=2025-09-01`
- Auth: `api-key: <key>` header (not `Authorization`) for API key; `Authorization: Bearer <token>` for bearer token
- Trace correlation: `x-ms-client-request-id` header
- Normalizes `response["value"]` array into `results`, `@odata.count` into `count`, `@search.facets` into `facets`
- `$top` is capped at `min(int(top), 1000)` to stay within Azure limits
- Supports: `$search`, `$filter`, `$orderby`, `$select`, `$facets`/`facet`, `$top`, `$skip`, `$count`
- Default `api_version = "2025-09-01"` — update when upgrading Azure AI Search tier

**`ElasticsearchSearchBackend`:**
- Issues `POST /{index}/_search` with `?routing={tenant_id}` when routing key is present
- Auth: `Authorization: ApiKey <key>` header
- Trace correlation: `X-Opaque-Id` header (appears in ES slow logs and task API)
- Always sets `track_total_hits: true` so `count` is exact, not approximate
- Normalizes `hits.hits[].\_source` into `results`, extracts `_score` into each result, `hits.total.value` into `count`, `aggregations` into `facets`
- Index resolution: uses `alias` from request (set by tenant policy engine) if present, else falls back to `index_names_by_entity_type` or `default_index_name`

**Request shape entering each backend:**

```python
# Azure backend receives:
{
    "params": {"$search": "...", "$filter": "...", "$top": "10"},
    "tenant_id": "acme",
    "entity_type": "customerDocument",
    "trace_id": "uuid-...",
}

# Elasticsearch backend receives (built by service._build_elastic_request()):
{
    "tenant_id": "acme",
    "entity_type": "customerDocument",
    "alias": "customerDocument-shared_a-read",  # from TenantPolicyEngine
    "routing": "acme",                           # tenant_id for shared; None for dedicated
    "trace_id": "uuid-...",
    "query": {                                   # built by ODataTranslator
        "query": {"bool": {"must": [...]}},
        "size": 10,
    },
}
```

#### Load-Test Harness (`harness.py`)

`run_search_gateway_harness()` is the async harness runner used for performance and regression testing. It can also be invoked as a CLI via `python -m unified_modernization.gateway.harness`.

**Key types:**
```python
SearchHarnessCase(
    name="customer-search",
    consumer_id="web-frontend",   # used for deterministic canary bucketing
    tenant_id="tenant-a",
    entity_type="customerDocument",
    raw_params={"$search": "gold", "$top": "10"},
    weight=3,                     # replicated 3× per iteration
)

SearchHarnessConfig(
    concurrency=8,       # asyncio.Semaphore concurrency (default 8)
    iterations=1,        # how many times to replay the full case set
    warmup_iterations=0, # warm-up passes excluded from the report
    max_error_samples=10,
)
```

**Report shape — `SearchHarnessReport`:**
- `total_requests`, `successful_requests`, `failed_requests`
- `average_latency_ms`, `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`, `min_latency_ms`, `max_latency_ms`
- `average_result_count`
- `shadow_backend_failures`, `shadow_regressions`, `canary_auto_disabled`
- `canary_frozen` — reflects live service state at end of run
- `error_samples` — up to `max_error_samples` error details for debugging

**Percentile calculation:** uses linear interpolation between adjacent sorted values. P95 of 100 requests = value at position 94.05 (interpolated between index 94 and 95).

**Weight expansion:** a case with `weight=3` and `iterations=2` produces 6 requests (3 per iteration). Warmup requests run before the baseline counter snapshot so their telemetry is excluded from the report delta.

**CLI usage:**
```bash
python -m unified_modernization.gateway.harness \
    --cases-file examples/search_harness_cases.jsonl \
    --concurrency 16 \
    --iterations 5 \
    --warmup-iterations 1 \
    --output report.json
```

#### Resilience (`resilience.py`)

Every backend is wrapped in `ResilientSearchBackend`. The `name` parameter is **required** (used in telemetry tags):

```python
ResilientSearchBackend(
    backend=AzureAISearchBackend(config),
    name="azure",                        # required — used in telemetry tags
    timeout_seconds=1.0,                 # default: 1.0s per attempt (standalone default)
    max_retries=1,                       # default: 1 retry (standalone default)
    retry_backoff_seconds=0.05,          # default: 50ms × attempt number
    failure_threshold=3,                 # default: 3 (standalone); GatewayRuntimeConfig default: 5
    recovery_timeout_seconds=30.0,       # default: 30s before HALF_OPEN probe
    telemetry_sink=sink,
)
```

**`GatewayRuntimeConfig` wired defaults** (used when instantiated via `build_http_search_gateway_service_from_env()`):

| Parameter | `GatewayRuntimeConfig` default | Env var |
|---|---|---|
| `azure_timeout_seconds` | `2.0` | `UMP_GATEWAY_AZURE_TIMEOUT_SECONDS` |
| `elastic_timeout_seconds` | `1.0` | `UMP_GATEWAY_ELASTIC_TIMEOUT_SECONDS` |
| `max_retries` | `2` | `UMP_GATEWAY_MAX_RETRIES` |
| `failure_threshold` | `5` | `UMP_GATEWAY_FAILURE_THRESHOLD` |
| `recovery_timeout_seconds` | `30.0` | `UMP_GATEWAY_RECOVERY_TIMEOUT_SECONDS` |

Circuit breaker states and transitions:
```
CLOSED ──(failure_threshold consecutive failures)──► OPEN
OPEN   ──(recovery_timeout_seconds elapsed)        ──► HALF_OPEN (probe attempt)
HALF_OPEN ──(probe succeeds)                        ──► CLOSED
HALF_OPEN ──(probe fails — immediately)             ──► OPEN  ← no threshold; any failure reopens
```

**HALF_OPEN failure behavior (important):** If a single probe attempt fails while in `HALF_OPEN`, the circuit goes directly back to `OPEN` without needing to accumulate `failure_threshold` failures again. This prevents a partially recovered backend from receiving sustained traffic prematurely.

Backoff uses linear scaling: `await asyncio.sleep(retry_backoff_seconds * attempt)` — so attempt 1 waits 50ms, attempt 2 waits 100ms.

Telemetry emitted by `ResilientSearchBackend`:
- `search.backend.success` — counter, tagged `{backend}`
- `search.backend.failure` — counter, tagged `{backend}`
- `search.backend.latency_ms` — timing, tagged `{backend, outcome}`
- `search.backend.circuit_rejected` — counter when circuit is OPEN and request blocked
- `search.backend.circuit_opened` — counter when circuit transitions to OPEN

#### Shadow Quality Gate (`evaluation.py`)

`ShadowQualityGate.evaluate()` runs **two mutually exclusive evaluation paths** depending on whether a `QueryJudgment` was provided:

**Path A — Judgment-based (NDCG/MRR):** fires when `primary_metrics` AND `shadow_metrics` are both non-None:
- If `shadow_ndcg_at_10 < min_shadow_ndcg_at_10` OR `ndcg_drop > max_ndcg_drop` OR `shadow_mrr < min_shadow_mrr` → `ShadowQualityEvent(code="shadow_relevance_regression", severity="high")`

**Path B — Live comparison only (overlap):** fires when no judgment is available:
- If `overlap_rate_at_k < min_overlap_rate_at_k` → `ShadowQualityEvent(code="shadow_overlap_regression", severity="medium")`

**Actual default thresholds:**
```python
ShadowQualityGate(
    min_overlap_rate_at_k=0.5,     # default: 50% overlap (Path B only)
    min_shadow_ndcg_at_10=0.85,    # default: shadow NDCG@10 must be >= 0.85 (Path A)
    max_ndcg_drop=0.10,            # default: max allowed NDCG drop vs primary (Path A)
    min_shadow_mrr=0.5,            # default: shadow MRR must be >= 0.5 (Path A)
)
```

Example with tighter thresholds for a high-recall domain:
```python
ShadowQualityGate(
    min_overlap_rate_at_k=0.8,
    min_shadow_ndcg_at_10=0.90,
    max_ndcg_drop=0.05,
    min_shadow_mrr=0.7,
)
```

When a regression event fires and mode is `CANARY`, the canary is auto-disabled.

---

### 5.7 Routing — Tenant Policy Engine

**Location:** `src/unified_modernization/routing/tenant_policy.py`

Two engines handle tenant-aware routing:

#### `TenantPolicyEngine` — Search Index Routing

Routes search writes and reads to the correct Elasticsearch alias:

| Tenant type | Write alias | Read alias | Routing key |
|---|---|---|---|
| Dedicated | `{entity_type}-{tenant_id}-write` | `{entity_type}-{tenant_id}-read` | None |
| Shared-A (hash%2==0) | `{entity_type}-shared_a-write` | `{entity_type}-shared_a-read` | tenant_id |
| Shared-B (hash%2==1) | `{entity_type}-shared_b-write` | `{entity_type}-shared_b-read` | tenant_id |

Dedicated tenants get their own physical indices (no routing key — all shards searched). Shared tenants use Elasticsearch `routing` to pin their documents to specific shards, keeping per-tenant query costs bounded.

#### `IngestionPartitionPolicyEngine` — Message Broker Partitioning

Assigns each `(tenant_id, entity_type)` pair to an ingestion partition:

| Tenant type | Partition key | Partition group |
|---|---|---|
| Whale (high-volume) | `{entity_type}-{tenant_id}:dedicated` | `{entity_type}-{tenant_id}` |
| Shared | `{entity_type}-shared-{N:02d}:{tenant_id}` | `{entity_type}-shared-{N:02d}` |

`N = SHA256(tenant_id:entity_type) % shared_partition_count` (default 32). Whale tenants get dedicated Kafka partitions to prevent noisy-neighbor effects.

---

### 5.8 Reconciliation — Anti-Entropy Engine

**Location:** `src/unified_modernization/reconciliation/engine.py`

The reconciliation engine detects drift between the source store and the target (Elasticsearch) index.

#### Simple Engine (`ReconciliationEngine`)

Flat comparison of two `StoreSnapshot` objects:
- Count mismatch
- Missing documents (in source but not target)
- Unexpected documents (in target but not source)
- Checksum drift (document exists in both but content differs)
- Delete-state mismatch (one store has document as deleted, other as active)

#### Bucketed Engine (`BucketedReconciliationEngine`)

Production-scale anti-entropy using a hash-first approach:

1. **Build snapshots:** Each store hashes its documents into `bucket_count` buckets (default **1024**). Bucket assignment uses `MD5(doc_id) % bucket_count`, formatted as `f"bucket-{bucket:0{width}d}"`. Each bucket's `aggregate_checksum` is computed as **SHA-256 of sorted entry lines** (`"doc_id|checksum|tenant_id|cohort|is_deleted"` joined by newlines) — NOT XOR. This means the checksum detects both content drift AND ordering changes within a bucket.
2. **Compare digests:** If `aggregate_checksum` matches for a bucket, skip it entirely — O(bucket_count) phase.
3. **Drill down:** For mismatching buckets, fetch the actual document list and compare — O(documents_in_mismatch). Parameters: `max_drilldown_buckets=16`, `max_recursive_depth=3`, `target_leaf_size=128`. Child bucket IDs during drill-down use `f"{parent_bucket_id}/child-{suffix:02d}"` with SHA-256.
4. **`ReconciliationReport.passed`** is `True` only when there are **no findings with `severity == "critical"`**. Non-critical findings still appear in the report.

This means a reconciliation run over 10M documents where only 100 documents drifted costs O(bucket_count + 100) not O(10M).

**Usage:**
```python
from unified_modernization.reconciliation.engine import (
    BucketedReconciliationEngine, BucketedStoreSnapshot, DocumentFingerprint
)

engine = BucketedReconciliationEngine()

source_snapshot = engine.build_snapshot(
    name="spanner",
    documents=[
        DocumentFingerprint(checksum="abc", tenant_id="t1", cohort="2024", is_deleted=False),
        ...
    ],
    bucket_count=1024,   # default; must match between source and target snapshots
)

target_snapshot = engine.build_snapshot("elasticsearch", documents=[...], bucket_count=1024)

report = engine.compare_snapshots(source_snapshot, target_snapshot)
print(f"passed={report.passed}")  # True only if no severity='critical' findings
for finding in report.findings:
    print(f"[{finding.severity}] {finding.code}: {finding.message}")
```

**Production use — `compare_remote_stores()`:**

For deployed environments, `BucketedReconciliationEngine` also provides `compare_remote_stores()`, which accepts two `RemoteBucketStore` instances (implementing the `RemoteBucketStore` protocol). This avoids loading all documents into memory by fetching only the aggregate checksums first, then drilling down into mismatching buckets on demand:

```python
# RemoteBucketStore protocol: fetch_aggregate_checksums(), fetch_bucket_documents()
report = engine.compare_remote_stores(
    source=SpannerRemoteBucketStore(spanner_client),
    target=ElasticsearchRemoteBucketStore(es_client),
    bucket_count=1024,
)
```

---

### 5.9 Backfill — Bulk Side-Load Coordinator

**Location:** `src/unified_modernization/backfill/coordinator.py`

`BackfillCoordinator` handles the initial bulk load before streaming catches up.

**Constructor:** takes only `projection_builder` — no publisher. The coordinator only builds projections, not publish them to Elasticsearch. Publishing is done by the `ProjectionRuntime` in streaming mode.

```python
coordinator = BackfillCoordinator(projection_builder=builder)  # no publisher parameter

result = coordinator.side_load(
    events=[...],                        # Iterable[CanonicalDomainEvent] (list or generator)
    captured_watermarks=[               # param name is captured_watermarks, not watermarks
        SourceWatermark(
            source_name="cosmos-customer",
            source_type="cosmos_change_feed",  # valid: cosmos_change_feed | spanner_change_stream
                                               #        pubsub_snapshot | debezium_offset | kafka_offset
            position="<current-continuation-token>",
        )
    ],
    checkpoint_store=store,              # optional CheckpointStore for resumable runs
    checkpoint_every=50_000,             # default: 50,000 events between checkpoints
    now_utc=datetime.now(UTC),
)

# result.summary: ingested_events, published_documents, deleted_documents, pending_documents
# result.handoff_plan.pending_documents: entities not yet fully assembled
# result.handoff_plan.captured_watermarks: where to start streaming consumers
# result.handoff_plan.instructions: 3 hardcoded operator instructions
```

**Resumable runs:** If `events` is a `Sequence` (e.g., a list), `side_load()` uses `events[start_from:]` slicing to skip already-processed events. If `events` is a plain iterator/generator, it uses `enumerate()` with a skip gate. This means passing a pre-sliced list is more memory-efficient than a generator for large backfills.

**Hardcoded instructions in `StreamHandoffPlan`:**
1. "Resume source CDC streams from the captured high watermarks, not from event zero."
2. "Send only delta changes through the streaming bus; historical records have already side-loaded into the projection store."
3. "Prioritize rehydration or repair events for entities still marked pending after bulk completion."

The `StreamHandoffPlan` is the bridge between backfill and streaming — it captures the CDC watermarks at the moment the bulk load completes so the streaming consumer knows exactly which offset to start consuming from.

---

### 5.10 Observability — Telemetry Abstractions

**Location:** `src/unified_modernization/observability/`

All platform components accept a `TelemetrySink` (Protocol) — never a concrete implementation. This allows telemetry to be swapped without touching business logic.

#### `TelemetrySink` Protocol

```python
class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...
    def increment(self, name: str, value: int = 1, tags: dict[str, str] | None = None) -> None: ...
    def record_timing(self, name: str, duration_ms: float, tags: ..., trace_id: ...) -> None: ...
    def start_span(self, name: str, trace_id: str | None, attributes: ...) -> TelemetrySpan: ...
```

#### Implementations

| Class | Mode | Use case |
|---|---|---|
| `NoopTelemetrySink` | `"noop"` | Maximum performance, no observability |
| `InMemoryTelemetrySink` | `"memory"` | Unit tests — inspect `.events`, `.counters`, `.timings` |
| `StructuredLoggerTelemetrySink` | `"logger"` | Local dev / staging — JSON log lines |
| `OpenTelemetryTelemetrySink` | `"otlp_http"` | Production — OTLP export to Datadog/Grafana/Jaeger |

#### `TelemetrySpan` context manager

```python
with sink.start_span("projection.builder.upsert", attributes={"entity_type": "customerDocument"}) as span:
    # __enter__: emits "{name}.start" event with attributes
    # __exit__ success: records timing, emits "{name}.finish" with outcome="ok", severity="info"
    # __exit__ exception: records timing, emits "{name}.finish" with outcome="error",
    #                     error_type=ExceptionClassName, severity="error"
    #   *** Event type is ALWAYS "{name}.finish" — there is NO "{name}.error" event type ***
    # span.trace_id  →  auto-generated UUID if not provided at construction
    do_work()
```

The span **does not suppress exceptions** (`__exit__` always returns `False`). It records the exception in telemetry but re-raises it to the caller.

#### Key metrics emitted by the platform

**Projection pipeline:**

| Metric | Type | Description |
|---|---|---|
| `projection.upsert.total` | Counter | All projection attempts, tagged by `published`, `change_type` |
| `projection.completeness.achieved` | Counter | Entities that transitioned to PUBLISHED/DELETED |
| `projection.time_to_completeness` | Timing | Time from earliest fragment `event_time_utc` to publish processing time (ms); includes historical age during backfill |
| `projection.quarantined` | Counter | Entities quarantined |
| `projection.failed` | Counter | Projection errors sent to DLQ |
| `projection.publish_failed` | Counter | Elasticsearch write errors sent to DLQ |
| `projection.published` | Counter | Successful publishes |
| `projection.pending` | Counter | Upserts that returned `publish=False` |
| `projection.backpressure.rejected` | Counter | Events throttled by backpressure |
| `projection.backpressure.bypassed` | Counter | Events bypassing backpressure, tagged `reason` |
| `projection.quarantine_failed` | Counter | Quarantine operation itself failed |

**Publisher:**

| Metric | Type | Description |
|---|---|---|
| `projection.publisher.indexed` | Counter | Successful single-document index writes |
| `projection.publisher.deleted` | Counter | Successful single-document deletes |
| `projection.publisher.bulk_requests` | Counter | Bulk API calls made |
| `projection.publisher.bulk_failures` | Counter | Individual doc failures within bulk response |

**Search gateway and backends:**

| Metric | Type | Description |
|---|---|---|
| `search.gateway.requests` | Counter | All gateway requests, tagged by `mode`, `entity_type` |
| `search.shadow.regression` | Counter | Quality gate failures, tagged `code` |
| `search.gateway.canary_auto_disabled` | Counter | Canary freeze events |
| `search.shadow.backend_failure` | Counter | Shadow backend errors |
| `search.gateway.canary_frozen` | Counter | Requests served while canary is frozen |
| `search.shadow.order_mismatch` | Counter | Primary/shadow result order differs |
| `search.gateway.canary_routed` | Counter | Requests routed to Elastic (or Azure) by canary |
| `search.gateway.shadow_sampled_out` | Counter | Shadow skipped by observation sampling |
| `search.backend.success` | Counter | Successful backend queries, tagged `{backend}` |
| `search.backend.failure` | Counter | Failed backend queries, tagged `{backend}` |
| `search.backend.latency_ms` | Timing | Per-attempt latency, tagged `{backend, outcome}` |
| `search.backend.circuit_rejected` | Counter | Requests blocked by open circuit |
| `search.backend.circuit_opened` | Counter | Circuit transitions to OPEN |

---

## 6. End-to-End Data Flow: Event Ingestion Path

This walk-through traces a single change event from database to search index.

```
1. A row is updated in Azure SQL
   └── Debezium detects the change, emits a Kafka message:
       {
         "payload": {
           "op": "u",
           "after": {"id": "cust-123", "tenant_id": "acme", "lsn": "0/2F000", ...},
           "source": {"lsn": "0/2F000", "ts_ms": 1700000000000},
           "ts_ms": 1700000000000
         }
       }

2. Your consumer reads the Kafka message and calls:
   event = adapter.normalize(raw_record)
   # Returns CanonicalDomainEvent:
   #   domain_name="customer_documents", entity_type="customerDocument"
   #   logical_entity_id="cust-123", tenant_id="acme"
   #   source_version=196608  (0x2F000 as int)
   #   fragment_owner="document_core", change_type=UPSERT
   #   payload={...Azure SQL fields...}

3. Consumer passes event to runtime:
   result = runtime.process(event, now_utc=datetime.now(UTC))

4. ProjectionRuntime checks backpressure:
   pending = builder.pending_count()  # server-side COUNT from Spanner
   if pending > max_pending_documents and event.change_type not in {REPAIR, REFRESH}:
       result.throttled = True
       return result  ← event dropped; caller must retry or send to queue

5. ProjectionRuntime calls builder.upsert(event):

   5a. Resolve DependencyPolicy for (domain_name, entity_type)
   5b. Load entity from Spanner via mutate_entity():
       Entity: {fragments: {"document_core": <old>, "customer_profile": <existing>}, state: PUBLISHED}
   5c. LSN guard: 196608 >= old_source_version? Yes → accept new fragment
   5d. Check missing required fragments:
       rules = [document_core(required), customer_profile(required), optional_tags(optional)]
       Missing? No — both required fragments present
   5e. Check stale required fragments:
       customer_profile.event_time_utc = 12 minutes ago, TTL=900s? Not stale
       document_core.event_time_utc = now? Not stale
   5f. Merge payloads (rule order):
       merged = {**document_core.payload, **customer_profile.payload, **optional_tags.payload}
   5g. Check delete flags: not all deleted → proceed to publish
   5h. Compute payload hash: SHA-256(sort_keys=True)
       Same hash as before? → no-op (projection_version unchanged)
       Different hash? → projection_version += 1
   5i. Build SearchDocument:
       {document_id="cust-123", projection_version=7, completeness_status=COMPLETE, payload=merged}
   5j. Return PublicationDecision(publish=True, document=SearchDocument)

6. ProjectionRuntime sees publish=True → calls publisher.publish(document):
   PUT /customerDocument-shared_a-write/_doc/cust-123
       ?version=7&version_type=external
   Body: {tenant_id, domain_name, entity_type, ...merged payload fields}

7. Elasticsearch accepts the write (or rejects with 409 if version too old — safe idempotency)

8. Telemetry emitted:
   - projection.upsert.total{published=true} += 1
   - projection.completeness.achieved (if first time entering PUBLISHED state)
   - projection.time_to_completeness = 350ms
```

---

## 7. End-to-End Data Flow: Search Query Path

```
1. Translation surface path (`asgi.py`):
   POST /translate
   Headers: X-API-Key: <key>, Content-Type: application/json
   Body: {
     "params": {
       "$search": "gold customer",
       "$filter": "Status eq 'ACTIVE' and Tier gt 3",
       "$top": "10"
     }
   }

2. APIKeyAndBodyLimitMiddleware:
   - Checks Content-Length <= max_body_bytes
   - Validates API key against valid_api_keys set in non-dev environments
   - Passes to ODataTranslator

3. ODataTranslator.translate(params):
   {
     "query": {
       "bool": {
         "must": [
           {"multi_match": {"query": "gold customer", "fields": ["_all"]}},
           {"term": {"status": "ACTIVE"}},
           {"range": {"tier": {"gt": 3}}}
         ]
       }
     },
     "size": 10
   }

4. Client receives translated Elasticsearch DSL:
   {
     "query": {...},
     "size": 10
   }
```

The routed search path is **not** served by `asgi.py`. It is executed by `SearchGatewayService.search(consumer_id, tenant_id, entity_type, raw_params)` when embedded in a custom service, harness, or deployment-specific transport:

```python
response = await service.search(
    consumer_id="web-frontend",
    tenant_id="acme",
    entity_type="customerDocument",
    raw_params={
        "$search": "gold customer",
        "$filter": "Status eq 'ACTIVE' and Tier gt 3",
        "$top": "10",
    },
)
```

```text
[SHADOW mode]
1. Build Azure request from raw_params and query Azure as primary
2. Build Elastic request by applying tenant routing + OData translation
3. Query Elastic as shadow if `shadow_observation_percent` samples the request in
4. Compare primary vs shadow result sets and evaluate quality gate
5. Return primary response to caller

[CANARY mode]
1. Bucket `tenant_id:consumer_id` to decide primary backend for this caller
2. Query the selected primary backend
3. Optionally query the shadow backend if observation sampling keeps the request
4. Auto-freeze canary if the shadow quality gate emits a regression event
```

The backend response returned to the caller is always the **primary** backend response for the current mode:

```json
{
  "results": [...],
  "count": 142,
  "facets": {...}
}
```

---

## 8. Cutover State Machine Walk-Through

**Scenario:** Migrating `customer_documents` domain from Azure to GCP.

```python
from unified_modernization.cutover.state_machine import (
    DomainMigrationState, BackendPrimaryState, SearchServingState,
    FirestoreCutoverStateStore
)

# Initialize (loads persisted state from Firestore)
state = DomainMigrationState(
    domain_name="customer_documents",
    store=FirestoreCutoverStateStore(firestore_client),
)
# state.backend_state = AZURE_PRIMARY
# state.search_state  = AZURE_SEARCH_PRIMARY_ELASTIC_DARK

# Phase 1: Start GCP warming
state.transition_backend(
    BackendPrimaryState.AZURE_PRIMARY_GCP_WARMING,
    operator="alice@corp.com",
    reason="GCP Spanner provisioned and validated"
)

# Phase 2: Simultaneously start Elastic shadow traffic
state.transition_search(
    SearchServingState.AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW,
    operator="bob@corp.com",
    reason="Elastic index populated and query parity verified"
)

# Phase 3: Advance search to canary after shadow NDCG validated
state.transition_search(
    SearchServingState.ELASTIC_CANARY,
    operator="alice@corp.com",
    reason="Shadow quality gate passing for 7 days"
)

# Phase 4: Backend validated, move GCP to primary with fallback window
state.transition_backend(
    BackendPrimaryState.GCP_SHADOW_VALIDATED,
    operator="alice@corp.com",
    reason="Data comparison report clean"
)
state.transition_backend(
    BackendPrimaryState.GCP_PRIMARY_FALLBACK_WINDOW,
    operator="alice@corp.com",
    reason="GCP primary starting, Azure on 48h standby"
)

# Emergency rollback if needed:
state.transition_backend(
    BackendPrimaryState.AZURE_PRIMARY,
    operator="oncall@corp.com",
    reason="P0 latency spike on Spanner, rolling back"
)
# → AZURE_PRIMARY (only valid rollback target from FALLBACK_WINDOW)

# Invalid transition — raises ValueError:
state.transition_backend(
    BackendPrimaryState.GCP_PRIMARY_STABLE,  # must go through FALLBACK_WINDOW first
    operator="alice@corp.com",
    reason="skip ahead"
)
# ValueError: Invalid transition: gcp_shadow_validated -> gcp_primary_stable.
#             Valid targets from this state: ['gcp_primary_fallback_window']
```

---

## 9. Projection Completeness Logic — Step by Step

This is the algorithm inside `ProjectionBuilder._mutate_entity()`:

```
INPUT: CanonicalDomainEvent, DependencyPolicy, current ProjectionEntityRecord

STEP 1 — LSN Guard (conditional fragment replacement — mutation ALWAYS continues)
  current_fragment = entity.fragments.get(event.fragment_owner)
  if current_fragment is None or incoming.source_version >= current_fragment.source_version:
      entity.fragments[event.fragment_owner] = new FragmentRecord(event)
  # *** IMPORTANT: if source_version < current, the fragment is NOT replaced,
  #     but execution continues to STEP 2 and beyond. The mutation NEVER
  #     short-circuits at STEP 1. This is intentional — even an out-of-order
  #     event re-evaluates completeness using existing fragments (e.g., a
  #     concurrent fragment for another owner may have just arrived). ***

STEP 2 — Check Missing Required Fragments
  for each rule in policy.rules where rule.required = True:
      if rule.owner not in entity.fragments:
          missing.append(rule.owner)
  if missing:
      state.status = PENDING_REQUIRED_FRAGMENT
      state.missing_required_fragments = missing
      RETURN (publish=False)

STEP 3 — Check Stale Required Fragments
  for each rule in policy.rules where rule.required = True:
      fragment = entity.fragments[rule.owner]
      if rule.freshness_ttl_seconds and (now - fragment.event_time_utc) > TTL:
          stale.append(rule.owner)
  if stale:
      state.status = PENDING_REHYDRATION
      state.stale_required_fragments = stale
      RETURN (publish=False)

STEP 4 — Merge Payloads
  merged_payload = {}
  source_versions = {}
  delete_flags = []
  for each rule in policy.rules (in order):
      fragment = entity.fragments.get(rule.owner)
      if fragment:
          merged_payload.update(fragment.payload)   ← later rules override earlier
          source_versions[rule.owner] = fragment.source_version
          delete_flags.append(fragment.delete_flag)

STEP 5 — Check All-Delete
  if all(delete_flags) and delete_flags is not empty:
      hash = SHA-256({"is_deleted": True, "source_versions": ...})
      if hash == state.last_payload_hash and state.status == DELETED:
          RETURN (publish=False)  ← idempotent delete
      state.status = DELETED
      state.projection_version += 1
      RETURN (publish=True, document={payload={"is_deleted": True}})

STEP 6 — Check Optional Publish Policy
  if not policy.allow_partial_optional_publish:
      if len(source_versions) < len(policy.rules):  ← optional fragments missing
          state.status = PENDING_REQUIRED_FRAGMENT
          RETURN (publish=False)
  else:
      if len(source_versions) < len(policy.rules):
          completeness_status = PARTIAL  ← publish anyway without optional fragments

STEP 7 — Idempotency Hash Check
  payload_hash = SHA-256(sort_keys=True, merged_payload)
  if payload_hash != state.last_payload_hash or state.status != PUBLISHED:
      state.projection_version += 1
      state.last_payload_hash = payload_hash
  state.status = PUBLISHED
  RETURN (publish=True, document=SearchDocument(merged_payload, projection_version))
```

**Idempotency:** The hash check in Step 7 is the primary idempotency mechanism — it happens **inside the builder**, before any publish call. If the same payload arrives twice while the entity is already `PUBLISHED`:
- `projection_version` is **not** incremented (stays at N)
- `publish=True` is still returned — the runtime will attempt the Elasticsearch write
- ES receives `PUT /_doc/{id}?version=N&version_type=external` — since ES already stores version N, it returns **409 VersionConflictEngineException**
- The publisher calls `raise_for_status()`, which raises on 409 — this is a **publish failure**, not a silent no-op
- The runtime writes the event+document to the DLQ on publish failure

**Practical implication:** True de-duplication of re-delivered events should be handled upstream (Kafka exactly-once semantics or an idempotent consumer pattern). The platform prevents silent data corruption through external versioning, but a duplicate event that causes a 409 will land in the DLQ and require operator review.

---

## 10. Tenant Routing Deep Dive

### Why tenant routing matters

A single Elasticsearch cluster serves all tenants. Without routing:
- Large tenants ("whales") scatter their documents across all shards, consuming cache on every shard for every query.
- Cross-shard fan-out on every query adds latency proportional to shard count.

### Solution: Dedicated indices for whales, shared with routing key for others

```
Dedicated tenant ("enterprise-001"):
  write_alias = "customerDocument-enterprise-001-write"
  read_alias  = "customerDocument-enterprise-001-read"
  routing_key = None  ← no routing, all documents on dedicated shards

Shared tenant ("startup-xyz"):
  TenantPolicyEngine._stable_hash("startup-xyz") % 2 == 0  → SHARED_A
  write_alias = "customerDocument-shared_a-write"
  read_alias  = "customerDocument-shared_a-read"
  routing_key = "startup-xyz"  ← Elasticsearch routes to specific shard(s)
```

**Stable hashing:** SHA-256 ensures the same tenant always maps to the same bucket across restarts and deployments. `hashlib.sha256(tenant_id.encode()).hexdigest()` as a large integer, modulo bucket count.

### Ingestion partitioning (message broker)

```
Whale tenant:
  partition_key   = "customerDocument-enterprise-001:dedicated"
  partition_group = "customerDocument-enterprise-001"
  dedicated = True

Shared tenant ("startup-xyz"):
  N = SHA256("startup-xyz:customerDocument") % 32 = 17
  partition_key   = "customerDocument-shared-17:startup-xyz"
  partition_group = "customerDocument-shared-17"
  dedicated = False
```

All events for `startup-xyz`'s `customerDocument` go to Kafka partition group `customerDocument-shared-17`, ensuring ordered delivery within the entity while mixing with other shared tenants in the same partition.

---

## 11. Configuration Reference

### Domain YAML Configuration

```yaml
domains:
  - name: <domain_name>               # Unique per deployment, e.g. "customer_documents"
    entity_type: <entity_type>        # camelCase type key, e.g. "customerDocument"
    current_source: <source>          # azure_sql | cosmos | spanner | firestore | alloydb
    target_store: <store>             # spanner | elasticsearch | azure_ai_search | firestore | bigquery
    routing:
      dedicated_tenants:              # List of tenant IDs that get dedicated indices
        - tenant-id-1
    projection_policy:
      allow_partial_optional_publish: false  # true = publish with optional fragments missing
      rules:
        - owner: <fragment_owner>     # Must match CanonicalDomainEvent.fragment_owner
          required: true              # false = optional fragment
          freshness_ttl_seconds: 900  # null = no freshness check
```

### Gateway Configuration — Complete Env Var Map

The runtime is split across **four component-level config surfaces** so each part can be configured independently and safely. The search service itself uses `UMP_GATEWAY_*` plus backend-specific `UMP_AZURE_SEARCH_*` and `UMP_ELASTICSEARCH_*` variables.

#### ASGI Translation Surface (`GATEWAY_*` — no `UMP_` prefix)

| Variable | Description | Default |
|---|---|---|
| `GATEWAY_ENVIRONMENT` | `dev`/`staging`/`prod`; `dev` skips API key auth | `dev` |
| `GATEWAY_API_KEYS` | Comma-separated valid API keys | — |
| `GATEWAY_MAX_BODY_BYTES` | Request body size limit (bytes) | `65536` |
| `GATEWAY_FIELD_MAP` | `AzureField=esField,...` OData->ES field renames | — |

#### SearchGatewayService (`UMP_GATEWAY_*` + `UMP_AZURE_SEARCH_*` + `UMP_ELASTICSEARCH_*`)

| Variable | Description | Default |
|---|---|---|
| `UMP_ENVIRONMENT` | Environment name (affects safety checks) | `dev` |
| `UMP_GATEWAY_MODE` | `azure_only`/`shadow`/`canary`/`elastic_only` | `azure_only` |
| `UMP_GATEWAY_CANARY_PERCENT` | Canary traffic percentage (0-100) | `0` |
| `UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION` | Auto-freeze canary on regression | `true` |
| `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` | % of requests that run shadow comparison | `25` |
| `UMP_GATEWAY_AZURE_TIMEOUT_SECONDS` | Per-request timeout for Azure backend | `2.0` |
| `UMP_GATEWAY_ELASTIC_TIMEOUT_SECONDS` | Per-request timeout for Elastic backend | `1.0` |
| `UMP_GATEWAY_MAX_RETRIES` | Retry count per backend request | `2` |
| `UMP_GATEWAY_FAILURE_THRESHOLD` | Consecutive failures before circuit OPEN | `5` |
| `UMP_GATEWAY_RECOVERY_TIMEOUT_SECONDS` | Seconds before HALF_OPEN probe | `30.0` |
| `UMP_GATEWAY_FIELD_MAP` | `AzureField=esField,...` gateway-level field renames | — |
| `UMP_DEDICATED_TENANTS` | Comma-separated dedicated tenant IDs | — |
| `UMP_AZURE_SEARCH_ENDPOINT` | Azure AI Search service URL | **required** |
| `UMP_AZURE_SEARCH_DEFAULT_INDEX` | Default index name | — |
| `UMP_AZURE_SEARCH_INDEX_MAP` | `entity_type=index_name,...` mapping | — |
| `UMP_AZURE_SEARCH_API_VERSION` | Azure Search REST API version | `2025-09-01` |
| `UMP_AZURE_SEARCH_API_KEY` | Azure Search admin key | — |
| `UMP_AZURE_SEARCH_BEARER_TOKEN` | Alternative bearer token | — |
| `UMP_AZURE_SEARCH_DOCUMENT_ID_FIELD` | Document ID field name | `id` |
| `UMP_ELASTICSEARCH_ENDPOINT` | Elasticsearch cluster URL | **required** |
| `UMP_ELASTICSEARCH_DEFAULT_INDEX` | Default index name | — |
| `UMP_ELASTICSEARCH_INDEX_MAP` | `entity_type=index_name,...` mapping | — |
| `UMP_ELASTICSEARCH_API_KEY` | Elasticsearch API key | — |
| `UMP_ELASTICSEARCH_BEARER_TOKEN` | Alternative bearer token | — |
| `UMP_ELASTICSEARCH_DOCUMENT_ID_FIELD` | Document ID field name | `id` |

#### Projection Publisher (`UMP_PUBLISHER_*`)

| Variable | Description | Default |
|---|---|---|
| `UMP_ENVIRONMENT` | Environment name used by bootstrap safety checks | `dev` |
| `UMP_PUBLISHER_ENDPOINT` | Elasticsearch cluster URL for writes | **required** |
| `UMP_PUBLISHER_API_KEY` | Elasticsearch API key | — |
| `UMP_PUBLISHER_BEARER_TOKEN` | Alternative bearer token auth | — |
| `UMP_PUBLISHER_REFRESH` | Refresh policy passed through to Elasticsearch | — |
| `UMP_DEDICATED_TENANTS` | Comma-separated dedicated tenant IDs | — |
| `UMP_PUBLISHER_WRITE_ALIAS_MAP` | `entity_type=write_alias,...` mapping | — |

---

## 12. Local Development Setup

### Prerequisites

- Python 3.11 or higher
- pip or uv

### Install

```bash
# Clone the repo
git clone <repo-url>
cd unified-modernization-platform

# Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install with dev dependencies
pip install -e ".[dev]"

# For GCP store support
pip install -e ".[dev,gcp]"

# For OpenTelemetry support
pip install -e ".[dev,telemetry]"
```

### Verify installation

```bash
python -c "from unified_modernization.contracts.events import CanonicalDomainEvent; print('OK')"
```

### Run the ASGI translation surface locally

The ASGI app is a translation-only service (OData → ES DSL). It does NOT take a `SearchGatewayService` parameter.

```bash
# Set required env vars (GATEWAY_* prefix — no UMP_)
export GATEWAY_ENVIRONMENT=dev   # 'dev' skips API key auth
export GATEWAY_FIELD_MAP=""      # optional: "Status=status,Tier=tier"

# Start the ASGI server (module-level app instance)
uvicorn unified_modernization.gateway.asgi:app --host 0.0.0.0 --port 8080
```

### Run the SearchGatewayService locally

```bash
# Set required env vars
export UMP_ENVIRONMENT=dev
export UMP_GATEWAY_MODE=azure_only
export UMP_AZURE_SEARCH_ENDPOINT=https://your-service.search.windows.net
export UMP_AZURE_SEARCH_API_KEY=your-key
export UMP_ELASTICSEARCH_ENDPOINT=http://localhost:9200

# Build the gateway service (for use in Python or harness testing)
python -c "
from unified_modernization.gateway.bootstrap import build_http_search_gateway_service_from_env
service = build_http_search_gateway_service_from_env()
print('Gateway mode:', service.mode)
"
```

### Run projection builder (minimal example)

```python
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.contracts.projection import DependencyPolicy, DependencyRule
from unified_modernization.contracts.events import (
    CanonicalDomainEvent, SourceTechnology, ChangeType, MigrationStage
)

policy = DependencyPolicy(
    entity_type="customerDocument",
    rules=[DependencyRule(owner="document_core", required=True)],
)

builder = ProjectionBuilder(policies=[policy], environment="dev")

event = CanonicalDomainEvent(
    domain_name="customer_documents",
    entity_type="customerDocument",
    logical_entity_id="cust-123",
    tenant_id="acme",
    source_technology=SourceTechnology.COSMOS,
    source_version=1,
    fragment_owner="document_core",
    payload={"name": "ACME Corp", "status": "ACTIVE"},
    change_type=ChangeType.UPSERT,
    migration_stage=MigrationStage.AZURE_PRIMARY,
)

decision = builder.upsert(event)
print(decision.publish)    # True
print(decision.document)   # SearchDocument with merged payload
```

---

## 13. Running the Test Suite

```bash
# Run all 100 tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_projection.py -v

# Run a specific test
pytest tests/test_integrations.py::test_debezium_change_event_adapter_parses_postgres_lsn -v

# Type checking
mypy src/

# Linting
ruff check src/ tests/

# Security scan
bandit -r src/
```

### Test structure

| Test file | What it covers |
|---|---|
| `test_asgi.py` | ASGI middleware: auth fail-closed, body limits, health endpoint |
| `test_backfill.py` | Backfill coordinator: side-load, checkpointing, handoff plan |
| `test_config.py` | YAML config loading, policy extraction, unknown store rejection |
| `test_cutover.py` | FSM transitions, invalid transition rejection, Firestore transactional store |
| `test_deployment_harness.py` | Gateway harness: latency percentiles, shadow metrics, canary status |
| `test_gateway.py` | OData translation, shadow quality gate, circuit breaker, canary freeze |
| `test_integrations.py` | 23 end-to-end scenarios: all adapters, publisher, runtime, DLQ bootstrap |
| `test_observability.py` | OTel span events, exception recording, severity routing |
| `test_projection.py` | Builder logic: completeness, time-to-completeness, hash idempotency |
| `test_projection_runtime.py` | Async off-loop guarantee, backpressure, DLQ |
| `test_reconciliation.py` | Bucket mismatch, drill-down, paginated fetch, many mismatch |
| `test_routing.py` | Dedicated vs shared routing, stable hash bucketing, ingestion partitioning |

### Key test patterns

**`_ThreadAwareBuilder`** (`test_projection_runtime.py`): Raises `AssertionError` if store methods are called on the event loop thread. Verifies that `process_async()` uses `asyncio.to_thread()` correctly.

**`_FakeSpannerSnapshot`** (`test_projection.py`): Routes SQL patterns to in-memory fake data. The `COUNT(*) AS pending_count` pattern is explicitly handled to verify the server-side count query.

**`_CapturingHandler`** (`test_observability.py`): Captures `logging.LogRecord` objects to verify `levelno` values for severity routing tests.

---

## 14. Environment Variables Reference

Variables are split across four component-level config surfaces. `GATEWAY_*` (no prefix) is for the ASGI translation surface only. The routed search service uses `UMP_GATEWAY_*` plus backend-specific `UMP_AZURE_SEARCH_*` and `UMP_ELASTICSEARCH_*` variables. `UMP_PUBLISHER_*` and `UMP_TELEMETRY_*` configure the remaining runtime components.

### ASGI Translation Surface (`GATEWAY_*`)

| Variable | Description | Default |
|---|---|---|
| `GATEWAY_ENVIRONMENT` | `dev`/`staging`/`prod`; `dev` skips API key auth | `dev` |
| `GATEWAY_API_KEYS` | Comma-separated API keys | — |
| `GATEWAY_MAX_BODY_BYTES` | Max request body size (bytes) | `65536` |
| `GATEWAY_FIELD_MAP` | `AzureField=esField,...` field renames | — |

### SearchGatewayService (`UMP_GATEWAY_*` / `UMP_AZURE_SEARCH_*` / `UMP_ELASTICSEARCH_*`)

See Section 11 for the full gateway config map.

### Projection Publisher (`UMP_PUBLISHER_*`)

| Variable | Description | Default |
|---|---|---|
| `UMP_ENVIRONMENT` | Environment name (affects bootstrap safety checks) | `dev` |
| `UMP_PUBLISHER_ENDPOINT` | Publisher target cluster URL | **required** |
| `UMP_PUBLISHER_API_KEY` | Elasticsearch API key | — |
| `UMP_PUBLISHER_BEARER_TOKEN` | Alternative bearer token auth | — |
| `UMP_PUBLISHER_REFRESH` | Refresh policy applied to writes | — |
| `UMP_DEDICATED_TENANTS` | Comma-separated dedicated tenant IDs | — |
| `UMP_PUBLISHER_WRITE_ALIAS_MAP` | `entity_type=write_alias,...` mapping | — |

### Telemetry (`UMP_TELEMETRY_*`)

| Variable | Description | Default |
|---|---|---|
| `UMP_TELEMETRY_MODE` | `noop` \| `memory` \| `logger` \| `otlp_http` | `noop` |
| `UMP_TELEMETRY_SERVICE_NAME` | Service name emitted in OTel traces | `unified-modernization-platform` |
| `UMP_OTLP_COLLECTOR_ENDPOINT` | OTLP HTTP endpoint (**required** when mode is `otlp_http`) | — |
| `UMP_OTLP_HEADERS` | `key=value,key2=value2` headers for OTLP auth | — |

---

## 15. Production Deployment Checklist

Before deploying to production, verify each item:

### State Store Safety
- [ ] `ProjectionBuilder` is initialized with `SpannerProjectionStateStore`, not `InMemoryProjectionStateStore`
- [ ] Spanner tables created using `SpannerProjectionStateStore.projection_schema_ddl()`
- [ ] `DomainMigrationState` is initialized with `FirestoreCutoverStateStore`, not `InMemoryCutoverStateStore`

### Gateway Security
- [ ] `GATEWAY_ENVIRONMENT=prod` (not `dev`) — `dev` bypasses API key auth
- [ ] `GATEWAY_API_KEYS` set to non-empty comma-separated list of valid keys
- [ ] API key distribution via secrets manager (not environment variable literal in CI logs)
- [ ] `GATEWAY_MAX_BODY_BYTES` set appropriately for your payload sizes (default 64 KB)
- [ ] `UMP_GATEWAY_MODE` confirmed to reflect the current cutover FSM search state

### Projection Publisher
- [ ] `UMP_PUBLISHER_ENDPOINT` points to the write cluster or alias for the active target
- [ ] `UMP_PUBLISHER_API_KEY` or `UMP_PUBLISHER_BEARER_TOKEN` is set via secrets manager
- [ ] `UMP_PUBLISHER_WRITE_ALIAS_MAP` is configured for every entity type that uses write aliases
- [ ] `UMP_DEDICATED_TENANTS` matches the routing policy used by ingestion and gateway components

### Telemetry
- [ ] `UMP_TELEMETRY_MODE=otlp_http` (not `noop` or `memory`)
- [ ] `UMP_OTLP_COLLECTOR_ENDPOINT` points to a live collector (**required** when mode is `otlp_http` — raises `ValueError` if empty)
- [ ] `UMP_TELEMETRY_SERVICE_NAME` set to a meaningful service identifier
- [ ] Alert on `search.shadow.regression` counter
- [ ] Alert on `search.gateway.canary_auto_disabled` counter
- [ ] Alert on `projection.quarantined` counter

### Cutover State Machine
- [ ] Production state uses `FirestoreCutoverStateStore` with a verified Firestore instance
- [ ] Transition operations require human approval (not automated)
- [ ] Rollback procedure documented and tested in staging

### Backfill
- [ ] Backfill `side_load()` completes before streaming consumer is started
- [ ] `StreamHandoffPlan.captured_watermarks` used to configure streaming consumer start offsets
- [ ] `StreamHandoffPlan.pending_documents == 0` before advancing cutover FSM

### Publisher
- [ ] Elasticsearch external versioning (`version_type=external`) confirmed working
- [ ] Write aliases point to correct backing indices per tenant class
- [ ] Bulk publish error handling tested (partial failure scenarios)

---

## 16. Extending the Platform

### Adding a new CDC source adapter

1. Create `src/unified_modernization/adapters/my_source.py`
2. Implement `normalize(record: Mapping[str, object]) -> CanonicalDomainEvent`
3. Add appropriate config model (following `DebeziumChangeEventAdapterConfig` pattern)
4. Export from `src/unified_modernization/adapters/__init__.py`
5. Add tests in `tests/test_integrations.py`

### Onboarding a new domain

1. Add entry to `examples/domain_config.yaml` (or your deployment config YAML)
2. Define fragment owners matching your CDC adapter `fragment_owner` values
3. Set appropriate `freshness_ttl_seconds` based on your SLA
4. Deploy the config; no code change required for `ProjectionBuilder` — it reads policies dynamically

### Adding a new projection state store

1. Implement the `ProjectionStateStore` Protocol methods:
   - `get_state`, `save_state`, `get_fragments`, `upsert_fragment`, `mutate_entity`, `pending_count`
2. `mutate_entity` must be **atomic** — load and save in a single transaction
3. `pending_count` must use a **server-side COUNT** query, not client-side filtering (critical for backpressure accuracy at scale)

### Customizing the shadow quality gate

Default thresholds (see §5.6 for the evaluation path logic):
- `min_overlap_rate_at_k=0.5` — overlap check fires only when no judgment available
- `min_shadow_ndcg_at_10=0.85` — NDCG check fires only when judgment IS available
- `max_ndcg_drop=0.10` — max allowed NDCG regression vs primary
- `min_shadow_mrr=0.5` — minimum MRR for shadow backend

```python
from unified_modernization.gateway.evaluation import ShadowQualityGate

# Example: tighter thresholds for a high-recall financial search domain
gate = ShadowQualityGate(
    min_overlap_rate_at_k=0.8,    # raise overlap bar (judgment-free path)
    min_shadow_ndcg_at_10=0.90,   # raise NDCG floor (judgment path)
    max_ndcg_drop=0.05,           # tighten regression threshold
    min_shadow_mrr=0.7,           # raise MRR floor
)
```

Provide a `QueryJudgmentProvider` implementation to supply per-query relevance judgments for NDCG/MRR evaluation. Without judgments, only overlap rate is evaluated (Path B). Path B uses a lower default threshold (0.5) because overlap rate without position information is a noisier signal.

---

## 17. Failure Modes & Operational Runbooks

### Projection Failures

| Symptom | Root cause | Action |
|---|---|---|
| Entity stuck in `PENDING_REQUIRED_FRAGMENT` | A required fragment source has stopped producing events | Check CDC adapter logs for source. Send a `REPAIR` event to force re-evaluation once source resumes. |
| Entity stuck in `PENDING_REHYDRATION` | A required fragment arrived but is now older than `freshness_ttl_seconds` | Send a re-hydration event from the source with a current timestamp. |
| Entity stuck in `QUARANTINED` | A poison message caused an exception inside `mutate_entity()` | Inspect DLQ for the failing event. Fix the data issue. Manually re-enqueue a corrected event. |
| High `projection.backpressure.rejected` rate | `pending_count >= max_pending_documents` (default 100,000) | Investigate why entities are accumulating pending state. Scaling the consumer increases throughput. |
| `projection.publish_failed` rising | Publisher cannot reach Elasticsearch / 409 version conflict | Check ES cluster health. Review DLQ for failed event details. For 409, investigate whether the same entity is being concurrently updated. |

### Gateway Failures

| Symptom | Root cause | Action |
|---|---|---|
| `search.backend.circuit_rejected` rising | Circuit breaker OPEN — backend has accumulated `failure_threshold` failures | Backend is down or slow. Wait for `recovery_timeout_seconds` then monitor probe attempts. |
| `canary_auto_disabled=True` in harness report | Shadow quality gate fired — `ShadowQualityEvent(severity="high")` | Review shadow vs primary NDCG/MRR in telemetry. Re-enable canary manually after confirming query parity. |
| All requests returning 401 | `GATEWAY_ENVIRONMENT` is not `dev` but `GATEWAY_API_KEYS` is empty | Set `GATEWAY_API_KEYS` or set `GATEWAY_ENVIRONMENT=dev` for local testing. |
| OData translation returning unexpected DSL | Field map not configured or field names differ between Azure and ES | Set `GATEWAY_FIELD_MAP=AzureField=esField,...` to align field names. |

### Cutover State Machine Failures

| Symptom | Root cause | Action |
|---|---|---|
| `ValueError: Invalid transition` | Attempted to skip a state in the FSM | Follow the linear state progression. Use rollback only from `*_FALLBACK_WINDOW` states. |
| `ValueError: durable cutover state store is required` | `build_domain_migration_state()` called without a durable store outside `local/dev/test` | Pass a `FirestoreCutoverStateStore` in non-local environments. |

### Reconciliation Failures

| Symptom | Root cause | Action |
|---|---|---|
| `ReconciliationReport.passed=False` | `severity="critical"` findings detected | Review findings. `missing_in_target` means projection failed to publish. `checksum_drift` means published document differs from source. |
| Very high bucket mismatch count | All documents drifted or wrong `bucket_count` used | Ensure `bucket_count` is the same for both source and target snapshots. Verify CDC is not paused. |
| `compare_remote_stores()` fails immediately | `RemoteBucketStore` cannot connect to store | Check network access / credentials for both stores. |

---

## 18. Glossary

| Term | Definition |
|---|---|
| **CDC** | Change Data Capture — a pattern for streaming database changes as events |
| **Canonical event** | `CanonicalDomainEvent` — the single normalized event format all adapters output |
| **Fragment** | A partial view of an entity contributed by one data source (`fragment_owner`) |
| **Fragment owner** | A string identifier for a data source that contributes fields to an entity (e.g., `"document_core"`, `"customer_profile"`) |
| **LSN** | Log Sequence Number — a monotonic, database-assigned position in the transaction log used as `source_version` |
| **Projection** | An assembled, merged view of all fragments for one logical entity, ready for search indexing |
| **`mutate_entity()`** | The atomic read-modify-write operation in the state store — the only way to change projection state |
| **Backpressure** | Throttling of new events when `pending_count` exceeds `max_pending_documents` |
| **DLQ** | Dead Letter Queue — stores events that could not be processed after all retries |
| **Quarantine** | State an entity enters when a poison message is received; the entity is excluded from future normal processing until manually resolved |
| **Shadow mode** | Querying both primary and shadow backends on every request, returning primary response to the client while comparing results for quality analysis |
| **Canary mode** | Routing a configurable percentage of real traffic to the new (Elastic) backend |
| **NDCG@10** | Normalized Discounted Cumulative Gain at rank 10 — a ranked-retrieval quality metric (1.0 = perfect, 0.0 = worst) |
| **MRR** | Mean Reciprocal Rank — measures the rank of the first relevant result (1.0 = first position) |
| **External versioning** | Elasticsearch feature that rejects writes where the provided `version` is not greater than the stored version, enabling idempotent document updates |
| **Tenant routing class** | `DEDICATED` (own index) or `SHARED_A`/`SHARED_B` (shared index with routing key) |
| **Whale tenant** | A high-volume tenant that receives a dedicated Kafka partition to prevent noisy-neighbor effects |
| **Anti-entropy** | The process of detecting and resolving drift between two data stores |
| **Bucketed Merkle** | The reconciliation strategy: aggregate checksums by hash bucket, compare buckets first (O(buckets)), drill down only into mismatching buckets (O(drifted docs)) |
| **Cutover track** | One of two independent migration dimensions: `backend` (data store) or `search` (query serving) |
| **Migration stage** | The current phase of the backend migration, embedded in each canonical event: `AZURE_PRIMARY` → ... → `GCP_PRIMARY_STABLE` |
| **Ordering key** | `"{tenant_id}|{domain_name}|{logical_entity_id}"` — the message broker partition key that ensures causal ordering for a given entity |
| **OTLP** | OpenTelemetry Protocol — the standard wire protocol for exporting traces, metrics, and logs |
| **Circuit breaker** | Resilience pattern that stops calling a failing backend after a threshold of failures, giving it time to recover |

---

*Document generated from source at commit `44dc44e`. For questions, open an issue or reach the platform team.*
