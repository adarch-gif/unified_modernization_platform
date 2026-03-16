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
  INTERLEAVE IN PARENT projection_states ON DELETE CASCADE
