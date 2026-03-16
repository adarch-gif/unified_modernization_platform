# Domain Onboarding Template

Use this template before implementing a domain.

## Domain identity

- domain name
- entity types
- tenant model
- business criticality

## Current state

- current source technology
- current search indexes
- current consumer set
- current delete semantics

## Target state

- target operational store: Spanner / Firestore / AlloyDB
- justification for target choice
- expected change-publication pattern
- search routing class: shared or dedicated

## Search behavior

- fields used for search
- filters
- facets
- sort fields
- synonyms
- analyzers
- autocomplete
- vector or semantic features

## Readiness

- source volume
- update rate
- freshness requirement
- rollback requirement
- relevance corpus available
