CREATE TABLE projection_states (
  tenant_id STRING(MAX) NOT NULL,
  domain_name STRING(MAX) NOT NULL,
  entity_type STRING(MAX) NOT NULL,
  logical_entity_id STRING(MAX) NOT NULL,
  status STRING(MAX) NOT NULL,
  payload_json STRING(MAX) NOT NULL,
  commit_timestamp TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id)
