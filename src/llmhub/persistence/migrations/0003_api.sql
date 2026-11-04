-- T-369: API gateway columns and indexes.
ALTER TABLE jobs ADD COLUMN backend TEXT;   -- 'local:<name>' or 'cloud:<pool>' (X-LLMHub-Backend)
ALTER TABLE jobs ADD COLUMN warnings TEXT;  -- comma separated: secret_detected, economy_stage
CREATE INDEX jobs_local_queue ON jobs(status, prio_rank, created_at) WHERE pool IS NULL;
CREATE INDEX clients_key ON clients(key_hash);
