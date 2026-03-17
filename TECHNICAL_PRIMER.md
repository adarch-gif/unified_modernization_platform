# Unified Modernization Platform — Technical Primer

> **Audience:** product owners, operations leads, technical program managers, and engineers who need a confident system-level view without reading every module.
> **What this document is not:** a developer guide. For module-level details see `DEVELOPER_GUIDE.md`. For design rationale see `ARCHITECTURE_DECISIONS.md`. For deployment mechanics see `docs/TERRAFORM_DEPLOYMENT.md`.

---

## Table of Contents

1. [The System in One Paragraph](#1-the-system-in-one-paragraph)
2. [Component Map](#2-component-map)
3. [The Two Migration Tracks](#3-the-two-migration-tracks)
4. [How a Change Event Moves Through the System](#4-how-a-change-event-moves-through-the-system)
5. [How a Search Query Moves Through the System](#5-how-a-search-query-moves-through-the-system)
6. [Cutover and Rollback Control](#6-cutover-and-rollback-control)
7. [Reconciliation and Repair](#7-reconciliation-and-repair)
8. [Runtime Configuration Reference](#8-runtime-configuration-reference)
9. [Monitoring: What to Watch and What It Means](#9-monitoring-what-to-watch-and-what-it-means)
10. [Failure Scenarios and System Response](#10-failure-scenarios-and-system-response)
11. [Operational Runbooks](#11-operational-runbooks)
12. [Deployment Topology](#12-deployment-topology)

---

## 1. The System in One Paragraph

The Unified Modernization Platform moves production data and search serving from Microsoft Azure to Google Cloud Platform and Elasticsearch without stopping the application. It does this by running the old system and the new system simultaneously, continuously synchronising data from old to new, testing search quality under real traffic before customers see it, and providing operators with state machines that advance the migration one verified step at a time with a defined rollback path at every stage. The platform is a control backbone, not a monolithic service — it is a set of composable runtime components wired together by configuration.

---

## 2. Component Map

The platform has seven components. Every component has a single responsibility.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                     UNIFIED MODERNIZATION PLATFORM                           │
│                                                                              │
│  ┌────────────────┐   ┌──────────────────────────────────────────────────┐  │
│  │  SOURCE        │   │  PROJECTION PIPELINE                             │  │
│  │  ADAPTERS      │──►│                                                  │  │
│  │                │   │  ┌──────────────┐   ┌────────────────────────┐  │  │
│  │  Normalise raw │   │  │  Projection  │──►│  Elasticsearch         │  │  │
│  │  CDC events    │   │  │  Runtime     │   │  Document Publisher    │  │  │
│  │  from each     │   │  │              │   └────────────────────────┘  │  │
│  │  source into   │   │  │  Builder +   │                               │  │
│  │  one common    │   │  │  State Store │   ┌────────────────────────┐  │  │
│  │  format        │   │  │  + DLQ       │──►│  Dead Letter Queue     │  │  │
│  └────────────────┘   │  └──────────────┘   └────────────────────────┘  │  │
│                        └──────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  SEARCH GATEWAY                                                       │   │
│  │                                                                       │   │
│  │  /health   /translate   /search                                       │   │
│  │                                                                       │   │
│  │  ┌─────────────────────────────────────────────────────────────┐     │   │
│  │  │  SearchGatewayService                                        │     │   │
│  │  │  Traffic mode  ·  Canary routing  ·  Shadow evaluation       │     │   │
│  │  │  Quality gates  ·  Per-backend resilience                    │     │   │
│  │  └─────────────────────────────────────────────────────────────┘     │   │
│  │         │                              │                              │   │
│  │   Azure AI Search               Elasticsearch                        │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │  CUTOVER FSMs    │  │  RECONCILIATION  │  │  OBSERVABILITY           │  │
│  │                  │  │  ENGINE          │  │                          │  │
│  │  Backend track   │  │                  │  │  TelemetrySink protocol  │  │
│  │  Search track    │  │  Bucketed hash   │  │  Structured events       │  │
│  │  Per domain      │  │  comparison      │  │  OTLP bridge             │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### What each component does

**Source Adapters**
Each source database (Azure SQL, Cosmos DB, Spanner, Firestore) emits change notifications in its own proprietary format. The adapters are translators: each one converts one source format into the single internal event type the rest of the platform speaks. Adding a new source means adding one adapter; no other component changes.

**Projection Pipeline**
A single search document for, say, a customer record may have its data spread across several source tables. The pipeline assembles those pieces. It tracks which pieces (called *fragments*) have arrived, holds publication until the required fragments are all present and fresh, and writes the finished document to Elasticsearch. If something fails, the event goes to a Dead Letter Queue rather than being silently dropped.

**Search Gateway**
The HTTP service that consumer applications call for search. It decides which backend answers each request based on the current *traffic mode*. In shadow mode it silently compares both backends. In canary mode it routes a configured percentage of traffic directly to Elasticsearch as primary and freezes automatically if quality degrades.

**Cutover FSMs**
Formal state machines — one per migration track (backend, search) per domain — that record the migration's current position. Every step forward requires an operator to name themselves and state a reason. Every step is immutable in the log. The system rehydrates correctly after any restart.

**Reconciliation Engine**
A verification tool that compares what the source system holds against what the target system holds, without reading every document in the healthy case. It flags missing, extra, and drifted documents so operators can repair before traffic shifts.

**Observability**
Every significant decision emits a structured event. These flow to stdout logs, an in-process collector for tests, or an OpenTelemetry collector for production dashboards — configurable without touching application code.

---

## 3. The Two Migration Tracks

The platform separates the migration into two independent tracks that share an event backbone but advance on their own schedule.

```
BACKEND TRACK (data stores)
────────────────────────────────────────────────────────────────────────
 AZURE_PRIMARY
    │  (GCP provisioned, replication warm)
    ▼
 AZURE_PRIMARY_GCP_WARMING
    │  (shadow writes verified, reconciliation passes)
    ▼
 GCP_SHADOW_VALIDATED
    │  (reconciliation clean, operator decision)
    ▼
 GCP_PRIMARY_FALLBACK_WINDOW          ← rollback still a single command
    │  (stability window passes)
    ▼
 GCP_PRIMARY_STABLE  ✓

SEARCH TRACK (query serving)
────────────────────────────────────────────────────────────────────────
 AZURE_SEARCH_PRIMARY_ELASTIC_DARK
    │  (Elasticsearch deployed, receiving no traffic)
    ▼
 AZURE_SEARCH_PRIMARY_ELASTIC_SHADOW
    │  (shadow quality metrics pass threshold)
    ▼
 ELASTIC_CANARY
    │  (canary gates pass across all traffic percentages)
    ▼
 ELASTIC_PRIMARY_FALLBACK_WINDOW      ← rollback still a single command
    │  (stability window passes)
    ▼
 ELASTIC_PRIMARY_STABLE  ✓
```

**Why two tracks?** The data store migration and the search migration have different validation criteria, different risk profiles, and different business owners. Coupling them would force the slower track to block the faster one. A domain's search cutover can reach `ELASTIC_PRIMARY_STABLE` while its backend migration is still in `GCP_SHADOW_VALIDATED`.

---

## 4. How a Change Event Moves Through the System

*Analogy: an assembly line where parts from several suppliers arrive independently and are held until a complete kit can be boxed and shipped.*

```
Step 1  A record changes in Cosmos DB.
        Cosmos emits a change notification in its own proprietary format.

Step 2  The Cosmos source adapter translates it into a CanonicalDomainEvent.
        This is the single internal event format everything else understands.

        {
          domain:       customer_documents
          entity_id:    C-12345
          tenant_id:    acme-corp
          fragment:     customer_profile
          change_type:  UPSERT
          version:      8340012          ← native source version, not a clock
        }

Step 3  The ProjectionRuntime receives the event and checks backpressure.
        If pending documents for this domain exceed 100 000, the event is
        throttled and must be re-queued. REPAIR and REFRESH events bypass
        this limit because they resolve pending states.

Step 4  The ProjectionBuilder looks up the dependency policy for this domain.
        It records the fragment and asks: do we now have all required fragments
        for entity C-12345, and are they all within their freshness window?

        Required fragments present and fresh?
          YES → READY_TO_BUILD → assemble complete document
          NO  → PENDING_REQUIRED_FRAGMENT → wait

Step 5  When READY_TO_BUILD, the builder computes the assembled document.
        If the payload is identical to the last published version, the
        publication is skipped (no unnecessary Elasticsearch writes).

Step 6  The publisher writes the document to Elasticsearch using the
        tenant-routed write alias.
        On a version conflict (409), the event is routed to the DLQ for
        operator review rather than being silently overwritten.
```

**Fragment freshness:** Each fragment has an optional `freshness_ttl_seconds` in the domain config. A required fragment that is present but older than its TTL moves the entity to STALE and queues it for rehydration. This acts as an implicit heartbeat — if a source CDC pipeline stops silently, documents gradually become stale rather than remaining published with undetected stale content.

**Document statuses:**

| Status | Meaning |
|---|---|
| PENDING_REQUIRED_FRAGMENT | Waiting for a mandatory source fragment |
| PENDING_REHYDRATION | Needs a fresh load from source |
| READY_TO_BUILD | All required fragments present and fresh |
| PUBLISHED | Successfully indexed |
| STALE | One or more fragments exceed their freshness window |
| QUARANTINED | Manually held; requires operator review and release |
| DELETED | Source record deleted; document removed from index |

---

## 5. How a Search Query Moves Through the System

### The two deployed surfaces

The platform ships two distinct HTTP surfaces. They serve different purposes.

**Translation-only surface** (`/health`, `/translate`)
Accepts an OData query, returns the equivalent Elasticsearch DSL JSON. No backend calls. Suitable for client-side query development and debugging.

**Deployable HTTP gateway** (`/health`, `/translate`, `/search`)
The production Cloud Run service. Accepts a search request, routes it through `SearchGatewayService`, and returns results. This is the current Cloud Run deployment target.

### Traffic modes

| Mode | Who answers the customer | Shadow / comparison |
|---|---|---|
| `AZURE_ONLY` | Azure AI Search | none |
| `SHADOW` | Azure AI Search | Elasticsearch is queried for a configurable fraction of requests (`UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT`); customer never sees Elastic results |
| `CANARY` | Azure for (100 − N)%, Elasticsearch for N% — split is deterministic by request hash | the non-primary backend is queried for a configurable sample of those requests |
| `ELASTIC_ONLY` | Elasticsearch | none |

**Important:** SHADOW does not mean every request runs both backends. `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` controls what fraction of requests also run the shadow comparison. This lets operators control the additional load on Elasticsearch before it is production-sized.

**Important:** In CANARY mode, a deterministic subset of requests is routed *directly* to Elasticsearch as the primary response — not "Azure primary plus Elastic compare for everyone." Customers in that subset see Elasticsearch results.

### Search quality gates (shadow and canary)

The quality gate has two evaluation modes:

1. **Always-available live comparison** based on the IDs returned by Azure and Elasticsearch.
2. **Optional judged relevance comparison** when a `QueryJudgmentProvider` supplies a relevance judgment for the query.

| Metric | When available | What it measures | Default threshold |
|---|---|---|---|
| Overlap rate | Always | What fraction of the top results are identical between backends | ≥ 0.5 when judged metrics are unavailable |
| NDCG@10 | Only when a query judgment is available | Result ranking quality (higher = results are better ordered) | Shadow NDCG@10 must stay ≥ 0.85 and NDCG drop must stay ≤ 0.10 |
| MRR | Only when a query judgment is available | How often the best result appears near the top | ≥ 0.5 |

If any threshold is breached during canary mode, `canary_auto_disabled` fires, `canary_frozen` is set in memory, and primary canary routing to Elasticsearch stops until an operator resets the service. Sampled shadow comparisons may still continue while the service remains in CANARY mode. This is the automatic safety valve; it requires no human monitoring to engage.

### Per-backend resilience

Every call to Azure AI Search or Elasticsearch is wrapped with:
- **Timeout:** configurable per backend; slow calls are aborted
- **Retry with bounded linear backoff:** transient failures are retried with `retry_backoff_seconds * attempt`
- **Circuit breaker:** after a configurable number of consecutive failures, the backend is marked open; calls are rejected immediately; after a recovery window one probe call is attempted

Circuit breaker state is per process instance (not shared across Cloud Run instances). Also note an important runtime detail: sampled shadow work is still awaited on the request path today. That means shadow failures do not change the primary backend's answer, but they can increase caller-visible latency until the shadow timeout/retry budget is exhausted.

### OData translation

Consumer applications send Azure AI Search OData syntax (`$filter`, `$search`, `$orderby`, `$top`, `$skip`, `$facets`). The gateway translates these to Elasticsearch Query DSL before forwarding. Field names are remapped through a configurable dictionary, which prevents field-name injection and allows aliasing.

```
Consumer sends:  $filter=Status eq 'ACTIVE' and Score ge 80
Gateway sends to Elasticsearch:
  { "query": { "bool": { "must": [
      { "term": { "status": "ACTIVE" } },
      { "range": { "score": { "gte": 80 } } }
  ]}}}
```

---

## 6. Cutover and Rollback Control

### What a transition looks like

An operator advances a domain's migration by issuing a state transition with four required fields:

```
domain:    customer_documents
track:     search
to_state:  ELASTIC_CANARY
operator:  j.smith@company.com
reason:    Shadow quality stable for 72h; NDCG at 0.92, overlap 0.81; reconciliation clean.
```

The state machine validates that the transition is legal (you cannot skip states or go to an undefined next state), writes the event to Firestore, and the new state takes effect immediately.

### Audit trail

Every transition event is immutable. The Firestore store is append-only: events are written, never updated or deleted. The current state for any domain is derived by reading all transition events for that domain. This means:
- The full migration history for every domain is always recoverable
- Who approved each step, when, and why is permanently recorded
- The system rehydrates to exactly the correct state after any restart

### Rollback

A rollback is a transition in the reverse direction, subject to the same audit requirements. An operator issues a rollback transition with their identity and reason, and the traffic mode or data routing reverts immediately.

Within a *fallback window* (the period immediately after a `*_FALLBACK_WINDOW` state is entered), rollback is a single operator command with immediate effect. Once a domain reaches `*_STABLE`, rollback is still possible but requires re-executing the migration steps from the appropriate earlier stage.

---

## 7. Reconciliation and Repair

*Analogy: reconciling a bank statement by first checking whether monthly totals match, then only drilling into the months that don't.*

Reconciliation compares what exists in the source system against what exists in the target system. Rather than comparing every document, it uses bucketed fingerprints:

```
Both systems hash all documents into 1 024 buckets by document ID.

For each bucket, compute a single fingerprint (aggregate checksum).

Compare bucket fingerprints:
  ├── All match?  ──► reconciliation passes. No documents examined.
  └── Bucket N differs?  ──► fetch and compare documents in bucket N only.

Within a differing bucket, classify each discrepancy:
  ├── missing     — in source, absent from target
  ├── unexpected  — in target, absent from source
  ├── checksum drift  — same ID, different content
  └── delete-state mismatch  — active in one, deleted in other
```

**Operational consequence:** a migration that is 99.9% correct touches only 0.1% of buckets in detail. A clean migration verifies in seconds.

**Repair path:** discrepancies are resolved by reprojecting the affected entity IDs. Operators provide the list of drifted IDs; the projection engine processes a REPAIR event for each, rebuilding the document from current fragment state.

---

## 8. Runtime Configuration Reference

Configuration is split across four surfaces. Do not mix them.

### Translation-only surface — prefix `GATEWAY_`

| Variable | Purpose |
|---|---|
| `GATEWAY_ENVIRONMENT` | `local` / `dev` / `test` / `prod` |
| `GATEWAY_API_KEYS` | Comma-separated valid API keys |
| `GATEWAY_MAX_BODY_BYTES` | Maximum accepted request body size |
| `GATEWAY_FIELD_MAP` | JSON object mapping OData field names to Elasticsearch field names |

### Search gateway — prefix `UMP_`

| Variable | Purpose |
|---|---|
| `UMP_ENVIRONMENT` | `local` / `dev` / `test` / `prod` |
| `UMP_GATEWAY_MODE` | `AZURE_ONLY` / `SHADOW` / `CANARY` / `ELASTIC_ONLY` |
| `UMP_GATEWAY_CANARY_PERCENT` | Integer 0–100; fraction of requests routed to Elasticsearch as primary in CANARY |
| `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` | Integer 0–100; fraction of requests that also run the shadow comparison |
| `UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION` | `true` / `false`; whether quality gate breach freezes canary automatically |
| `UMP_GATEWAY_AZURE_TIMEOUT_SECONDS` | Per-request timeout for Azure AI Search |
| `UMP_GATEWAY_ELASTIC_TIMEOUT_SECONDS` | Per-request timeout for Elasticsearch |
| `UMP_GATEWAY_MAX_RETRIES` | Maximum retry attempts on transient backend failure |
| `UMP_GATEWAY_FAILURE_THRESHOLD` | Consecutive failures before circuit breaker opens |
| `UMP_GATEWAY_RECOVERY_TIMEOUT_SECONDS` | Seconds before circuit breaker attempts a probe call |
| `UMP_GATEWAY_FIELD_MAP` | JSON object mapping OData field names to Elasticsearch field names |
| `UMP_DEDICATED_TENANTS` | Comma-separated tenant IDs that receive dedicated Elasticsearch aliases |
| `UMP_AZURE_SEARCH_ENDPOINT` | Azure AI Search service URL |
| `UMP_AZURE_SEARCH_API_KEY` | Azure AI Search API key |
| `UMP_AZURE_SEARCH_DEFAULT_INDEX` | Default Azure index name |
| `UMP_ELASTICSEARCH_ENDPOINT` | Elasticsearch cluster URL |
| `UMP_ELASTICSEARCH_API_KEY` | Elasticsearch API key |
| `UMP_ELASTICSEARCH_DEFAULT_INDEX` | Default Elasticsearch index name |

### Projection publisher — prefix `UMP_PUBLISHER_`

| Variable | Purpose |
|---|---|
| `UMP_PUBLISHER_ENDPOINT` | Elasticsearch endpoint for document writes |
| `UMP_PUBLISHER_API_KEY` | API key for document writes |
| `UMP_PUBLISHER_BEARER_TOKEN` | Bearer token (alternative to API key) |
| `UMP_PUBLISHER_REFRESH` | Elasticsearch `refresh` parameter (`true` / `wait_for` / `false`) |
| `UMP_PUBLISHER_WRITE_ALIAS_MAP` | JSON object mapping domain names to write alias names |

### Telemetry — prefix `UMP_`

| Variable | Purpose | Values |
|---|---|---|
| `UMP_TELEMETRY_MODE` | Where telemetry is sent | `noop` · `memory` · `logger` · `otlp_http` |
| `UMP_TELEMETRY_SERVICE_NAME` | Service name tag on all emitted events | string |
| `UMP_OTLP_COLLECTOR_ENDPOINT` | OTLP HTTP collector URL (required when mode is `otlp_http`) | URL |
| `UMP_OTLP_HEADERS` | Additional HTTP headers for OTLP collector (e.g. auth) | JSON object |

**Telemetry modes:**

| Mode | Behaviour | Use when |
|---|---|---|
| `noop` | All events discarded | CI pipelines |
| `memory` | Events collected in-process; accessible in tests | Unit and integration tests |
| `logger` | Structured JSON written to stdout | Non-production |
| `otlp_http` | Events shipped to an OpenTelemetry collector | Production |

---

## 9. Monitoring: What to Watch and What It Means

### Projection pipeline

| Signal | Source | Healthy | Alert |
|---|---|---|---|
| `pending_count()` per domain | Operational store query / dashboard | < 50 000 | > 100 000 (backpressure will activate) |
| `projection.pending` | Telemetry counter | Reflects normal incomplete-entity churn | Sustained growth with flat `projection.published` suggests a dependency or freshness gap |
| `projection.backpressure.rejected` | Telemetry counter | 0 or low | Sustained increase means ingest rate exceeds processing capacity |
| `projection.failed` | Telemetry counter | 0 | Any non-zero count means projection mutation failed before publish |
| `projection.publish_failed` | Telemetry counter | 0 | Any non-zero count; inspect DLQ |
| `projection.time_to_completeness` | Telemetry timing | Tracks expected freshness window | During backfill this includes historical age — do not interpret as online latency |

### Search gateway

| Signal | Healthy | Alert |
|---|---|---|
| `search.shadow.regression` | Not emitted | Any emission; review shadow comparison details |
| `search.gateway.canary_auto_disabled` | Not emitted | Any emission; canary frozen, investigate before re-enabling |
| `search.backend.circuit_opened` | Not emitted | Any emission; a backend has entered failure mode |
| Shadow overlap rate | > 0.6 | < 0.5; may indicate index completeness gap, not necessarily ranking regression |
| p95 search latency (Azure) | < 200 ms | > 500 ms |
| p95 search latency (Elasticsearch) | < 150 ms | > 400 ms |

**Interpreting overlap rate:** low overlap can mean Elasticsearch results are ranked differently (a quality problem) *or* that Elasticsearch's index is behind the Azure index (an index completeness problem). Before acting on a low overlap signal, check whether projection publishing is keeping up.

**Interpreting `projection.time_to_completeness`:** this metric measures the elapsed time from a source event to a published document. During backfill it includes the age of the historical event (potentially hours or days), not just the pipeline processing time. Treat it as a pipeline-correctness signal, not an online latency metric.

### Cutover state

| Signal | Meaning |
|---|---|
| Domain unchanged for > 30 days | Migration may be stalled; check reconciliation and shadow quality |
| Rollback transition in Firestore log | A regression was detected; review the reason field for root cause |
| Reconciliation mismatch count > 0 | Do not advance cutover state until mismatches are resolved |

---

## 10. Failure Scenarios and System Response

### Source CDC pipeline pauses

**What happens:** Cosmos DB change feed or Azure SQL Debezium stream stops delivering events.

**System response:** No new events arrive. The projection engine drains its in-flight work. Published documents remain correct but become increasingly stale relative to the source as the silence continues. The gateway continues serving from whichever backend is primary — no customer impact if Azure is still primary.

**Operator action:** Monitor source system health. When CDC resumes, events flow again automatically. After an extended outage, run reconciliation before advancing the cutover state machine.

---

### Elasticsearch returns degraded search results

**What happens (shadow mode):** The shadow quality gate detects either an overlap regression or, when judged metrics are available, a relevance regression. A `search.shadow.regression` event is emitted. Azure remains the primary answer source, so search correctness does not flip to Elasticsearch. However, sampled shadow requests still execute on the request path, so customer-visible latency can rise while shadow queries retry or time out.

**What happens (canary mode):** `search.gateway.canary_auto_disabled` fires. Primary canary routing to Elasticsearch stops because the in-memory canary percentage is forced to zero. Azure handles the primary response for all requests again. Sampled shadow comparisons can still continue until the service is restarted or the mode is changed. The alert reaches the operations team.

**Operator action:**
1. Inspect `search.shadow.regression` events for which query patterns show the largest drop.
2. Identify the cause: index mapping change, query translation error, or index completeness lag.
3. Fix, redeploy if needed, and run the load-test harness to verify recovery.
4. Reset the canary freeze flag.
5. Resume canary at a lower percentage than the level at which the freeze triggered.

---

### Projection backlog grows unexpectedly

**What happens:** A bulk data import or runaway upstream process floods the event stream. Pending document count climbs toward 100 000.

**System response:** At the threshold, `ProjectionRuntime` activates backpressure: new events are returned as THROTTLED rather than processed. `REPAIR` and `REFRESH` events continue to process regardless. Throttled events must be re-queued by the caller.

**Operator action:**
1. Confirm whether the spike is expected (a scheduled bulk import) or anomalous.
2. Monitor `projection.backpressure.rejected` — if non-zero, the pipeline is saturated.
3. Monitor `projection.publish_failed` and DLQ depth in parallel.
4. Scale projection workers if available. When the spike subsides, backpressure releases automatically.
5. Replay dead-lettered events after the root cause is resolved.

---

### GCP Spanner (projection state) unavailable

**What happens:** A GCP regional incident makes Spanner unavailable. Projection state mutations fail.

**System response:** Events that cannot be written to Spanner are routed to the DLQ. The search gateway continues operating on its current traffic mode (mode is loaded at startup, not re-read from Firestore on every request). Azure continues serving all customer queries if Azure is the primary backend.

**Operator action:**
1. Do not advance the cutover state machine during the incident.
2. After Spanner recovers, verify projection state integrity with a reconciliation run.
3. Replay DLQ events for affected domains.
4. Resume normal operations only after reconciliation passes.

---

### A service instance restarts mid-migration

**What happens:** Cloud Run scales down and back up during normal autoscaling, or a deployment rolls out.

**System response:** The currently deployed HTTP gateway is stateless at startup: it rebuilds its runtime from environment variables and Secret Manager-backed credentials. It does not rehydrate projection state from Spanner or cutover state from Firestore on startup. Those durable stores still preserve the platform's broader state for the components that use them. In-flight HTTP requests fail or retry at the client layer; the gateway process itself simply restarts cleanly.

**Operator action:** None required. This scenario is expected and the platform is designed for it.

---

### Reconciliation finds mismatches before a planned cutover step

**What happens:** Pre-cutover reconciliation reports missing, unexpected, or drifted documents.

**System response:** The reconciliation engine reports the findings. It takes no automated remediation action — reconciliation is read-only.

**Operator action:**
1. Categorise the findings: missing (projection pipeline behind), unexpected (projection gap in delete handling), checksum drift (fragment or mapping issue).
2. For missing documents: inspect the DLQ and in-flight projection queue; trigger REPAIR events for affected entity IDs.
3. Re-run reconciliation after repair is complete.
4. Only advance the cutover state machine after reconciliation reports clean.

---

## 11. Operational Runbooks

### Before pilot traffic

- Verify Azure AI Search and Elasticsearch credentials are set and the gateway starts cleanly.
- Deploy the full HTTP gateway (`http_api.py`), not the translation-only surface (`asgi.py`).
- Keep `UMP_GATEWAY_MODE=AZURE_ONLY` until shadow observation is ready.
- Keep `gateway_allow_unauthenticated = false` in Terraform.
- Set `UMP_TELEMETRY_MODE=logger` at minimum; use `otlp_http` if a collector is available.

---

### Starting shadow observation

1. Set `UMP_GATEWAY_MODE=SHADOW`.
2. Set `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` to a value that generates enough signal without overloading Elasticsearch (start at 10–20%).
3. Confirm `search.shadow.regression` events are not firing.
4. Monitor p95 latency while shadow observation is enabled; sampled shadow requests are still on the request path in the current implementation.
5. Increase `UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT` to 100% once Elasticsearch is production-sized and latency remains acceptable.
6. Collect shadow quality data for at least 48 hours of stable operation before advancing to CANARY.

---

### Starting canary

Prerequisites before setting `UMP_GATEWAY_MODE=CANARY`:
- Shadow quality metrics stable for 48+ hours.
- Overlap rate consistently above 0.6.
- No unresolved `search.shadow.regression` events.
- Elasticsearch index completeness confirmed by reconciliation.

1. Set `UMP_GATEWAY_MODE=CANARY`.
2. Set `UMP_GATEWAY_CANARY_PERCENT=5` (start conservative).
3. Set `UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION=true`.
4. Monitor for `search.gateway.canary_auto_disabled` for 30 minutes.
5. Monitor p95 latency as well as regression events; sampled shadow comparisons still add request-path work.
6. If stable, increase to 10%, then 25%, 50%, 100% — waiting for stability confirmation at each step.

---

### Resetting a frozen canary

After `search.gateway.canary_auto_disabled` fires:

1. Confirm the root cause: check shadow quality events, inspect which query patterns regressed.
2. Fix the root cause (index mapping, query translation, or freshness issue).
3. Run the load-test harness against real backends; preserve the output as an evidence artifact.
4. Ensure `UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION` is still `true` in configuration if it was changed during diagnosis.
5. Restart the gateway to clear the in-memory frozen-canary state (and any in-memory circuit-breaker state). This restart also stops any residual sampled shadow path from continuing under the frozen state.
6. Resume canary at a percentage lower than the level where the freeze triggered.

---

### Advancing a cutover state

Prerequisites before any forward transition:
- Reconciliation passes with zero mismatches for the domain.
- Shadow or canary quality metrics stable for at least 48 hours.
- No open incidents affecting the source or target cloud for this domain.

1. Confirm the prerequisites with the data quality and search product leads.
2. Issue the transition (domain, track, target state, your identity, stated reason).
3. Monitor telemetry for 30 minutes after transition.
4. Document the transition in the migration status report.

---

### Rolling back a cutover state

1. Confirm whether the domain is within the fallback window (check the timestamp of the `*_FALLBACK_WINDOW` transition in Firestore vs. the agreed stability window duration).
2. Issue the rollback transition (domain, track, previous state, your identity, stated reason).
3. Verify in telemetry that traffic has reverted to the expected backend.
4. Notify stakeholders.
5. Investigate root cause before re-attempting forward progress.

---

### Processing the Dead Letter Queue

1. Query the DLQ for the affected domain.
2. Classify failures by error type: schema error, state store unavailable, version conflict (409), dependency missing.
3. For transient errors (store unavailable): re-queue events after confirming the store has recovered.
4. For 409 conflicts: review whether the DLQ event is older than the currently published version; if so it can be discarded.
5. For schema or dependency errors: fix the root cause (adapter update, policy correction), then replay.
6. Re-run reconciliation after replay to confirm index consistency.
7. Clear processed DLQ entries.

---

## 12. Deployment Topology

### What the repo deploys today

```
Terraform apply
     │
     ├── Cloud Run  ──── HTTP gateway service  (/health, /translate, /search)
     │                   Harness job (load testing)
     │
     ├── Spanner  ─────── Projection state store
     │                    projection_states table
     │                    projection_fragments table (interleaved)
     │
     ├── Firestore  ────── Cutover state store
     │                    Append-only transition log per domain
     │
     ├── Pub/Sub  ──────── Projection topics and subscriptions
     │                    (substrate for future projection worker)
     │
     ├── Artifact Registry  ── Container image repository
     │
     └── Secret Manager  ─── Credentials for Azure, Elasticsearch, OTLP
```

### What the repo does not deploy today

The Terraform stack provisions Pub/Sub substrate for a future projection-consumer worker, but no such worker process exists in the repo yet. The current deployment surface is the HTTP gateway plus the supporting GCP data stores.

### Startup behaviour by environment

| Environment | Invalid bootstrap | Missing credentials |
|---|---|---|
| `local` / `dev` / `test` | Logged; `/search` returns 503 | Service starts; `/search` returns 503 |
| `prod` | Startup fails; Cloud Run revision never becomes ready | Startup fails; Cloud Run revision never becomes ready |

The production fail-closed behaviour comes from application startup, not from an explicit Cloud Run readiness probe definition in Terraform. In `prod`, invalid gateway or telemetry bootstrap raises during app construction, the process fails to start, and the revision never becomes healthy enough to serve traffic.

---

## Related Documents

| Document | Audience | Content |
|---|---|---|
| `docs/EXECUTIVE_GUIDE.md` | Business stakeholders, executives | Business problem, solution, benefits, migration journey, governance |
| `DEVELOPER_GUIDE.md` | Engineers | Module layout, runtime surfaces, configuration, local dev, test suite |
| `ARCHITECTURE_DECISIONS.md` | Principal engineers, architects | Every significant architectural decision with context, alternatives, and consequences |
| `docs/TERRAFORM_DEPLOYMENT.md` | Engineers, DevOps | GCP deployment mechanics, Terraform variables, pilot rollout steps |
