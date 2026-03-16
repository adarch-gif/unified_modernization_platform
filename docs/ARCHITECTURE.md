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

### Search Gateway

The gateway preserves a stable client-facing contract and supports:

- Azure-only mode
- shadow mode
- canary mode
- Elastic-only mode

### Cutover state machines

Backend and search state machines are separate because unified platform does not mean big-bang cutover.

### Reconciliation

The reconciliation engine compares count and checksum evidence between systems. The current implementation is intentionally simple and should be extended to stratified and field-level validation for production use.

## Production evolution path

The code in this repo is a starter. The next production step is replacing in-memory state with a strongly consistent control-plane store, then wiring real adapters for Azure SQL, Cosmos, Spanner, Firestore outbox, and AlloyDB CDC.
