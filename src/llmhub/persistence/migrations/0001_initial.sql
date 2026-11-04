CREATE TABLE controller_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
) STRICT;  -- keys: schema_version, halted, halt_reason, safe_mode, heartbeat, boot_id,
           -- local_secret_id (sha256(secret)[:8]), last_backup_at, last_reconcile_at:<p>, ewma:<pool>:pp|tg

CREATE TABLE clients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL,
  default_policy TEXT NOT NULL DEFAULT 'BALANCED' CHECK (default_policy IN ('ECONOMY','BALANCED','QUALITY')),
  default_priority TEXT NOT NULL DEFAULT 'normal' CHECK (default_priority IN ('urgent','normal','low')),
  max_priority TEXT NOT NULL DEFAULT 'normal' CHECK (max_priority IN ('urgent','normal','low')),
  allowed_models_json TEXT NOT NULL DEFAULT '[]',
  budget_overrides_json TEXT,
  data_class TEXT NOT NULL DEFAULT 'private' CHECK (data_class IN ('private','cloud_allowed')),
  created_at REAL NOT NULL, disabled_at REAL
) STRICT;

CREATE TABLE instances (
  id TEXT PRIMARY KEY, op_id TEXT NOT NULL UNIQUE, pool TEXT NOT NULL, provider TEXT NOT NULL,
  provider_ref TEXT, label TEXT NOT NULL UNIQUE, offer_json TEXT, host_id TEXT, gpu TEXT, price_usd_h REAL,
  state TEXT NOT NULL CHECK (state IN ('STOPPED','PROVISIONING','BOOTING','LOADING_MODEL','READY','BUSY',
         'IDLE','STOPPING','PENDING_DELETE','LEAKED','ERROR')),
  state_since REAL NOT NULL, idle_since REAL, endpoint TEXT, tls_fingerprint TEXT,
  created_at REAL NOT NULL, create_called_at REAL, ready_at REAL, max_lifetime_at REAL NOT NULL,
  stop_after_current INTEGER NOT NULL DEFAULT 0, stop_reason TEXT,
  destroy_requested_at REAL, destroy_accepted_at REAL, destroy_attempts INTEGER NOT NULL DEFAULT 0,
  next_destroy_at REAL, zero_confirmed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, model_test_failures_json TEXT NOT NULL DEFAULT '[]',
  net_fail_since REAL, accrued_to REAL, last_error TEXT
) STRICT;
CREATE UNIQUE INDEX instances_one_live_per_pool ON instances(pool)
  WHERE state NOT IN ('STOPPED','PENDING_DELETE','LEAKED');
CREATE INDEX instances_state ON instances(state);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
  idem_key TEXT, idem_expires_at REAL, body_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
  priority TEXT NOT NULL CHECK (priority IN ('urgent','normal','low')),
  prio_rank INTEGER NOT NULL CHECK (prio_rank IN (0,1,2)),
  task_type TEXT, model_alias TEXT NOT NULL, policy TEXT NOT NULL, data_class TEXT NOT NULL,
  pool TEXT, deadline_at REAL, max_queue_wait_s REAL, timeout_s REAL,
  status TEXT NOT NULL CHECK (status IN ('submitted','queued','running','completed','failed','cancelled')),
  route TEXT NOT NULL, route_reason TEXT, fallback_reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2, restarts INTEGER NOT NULL DEFAULT 0,
  est_tokens_in INTEGER NOT NULL DEFAULT 0, est_tokens_out INTEGER NOT NULL,
  tokens_in INTEGER, tokens_out INTEGER, finish_reason TEXT,
  est_cost_usd REAL, cost_usd REAL, error_code TEXT,
  instance_id TEXT REFERENCES instances(id), worker_id TEXT, worker_heartbeat_at REAL,
  req_path TEXT NOT NULL, res_path TEXT, started_at REAL, finished_at REAL,
  cancel_requested INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE UNIQUE INDEX jobs_idem ON jobs(client_id, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX jobs_pool_queue ON jobs(pool, status, prio_rank, created_at);
CREATE INDEX jobs_body ON jobs(client_id, body_sha256, status, finished_at);
CREATE INDEX jobs_instance ON jobs(instance_id, status);

CREATE TABLE leases (
  id TEXT PRIMARY KEY, holder TEXT NOT NULL, client_id TEXT REFERENCES clients(id),
  kind TEXT NOT NULL CHECK (kind IN ('implicit','explicit','hold','prewarm')),
  pool TEXT NOT NULL, job_id TEXT REFERENCES jobs(id), ttl_s REAL, expires_at REAL,
  created_at REAL NOT NULL, renewed_at REAL, closed_at REAL, close_reason TEXT,
  final INTEGER NOT NULL DEFAULT 0,
  CHECK ((kind = 'implicit') = (job_id IS NOT NULL)),
  CHECK (kind = 'implicit' OR expires_at IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX leases_one_per_job ON leases(job_id) WHERE kind = 'implicit';
CREATE INDEX leases_live ON leases(pool, closed_at, expires_at);

CREATE TABLE transitions (
  id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES instances(id), ts REAL NOT NULL,
  from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('controller','reaper','admin'))
) STRICT;
CREATE INDEX transitions_inst ON transitions(instance_id, ts);

CREATE TABLE ledger (
  id INTEGER PRIMARY KEY, instance_id TEXT REFERENCES instances(id), provider TEXT NOT NULL, pool TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('gpu','disk','traffic','storage')),
  from_ts REAL NOT NULL, to_ts REAL NOT NULL, rate REAL, amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
  source TEXT NOT NULL CHECK (source IN ('estimate','provider')), created_at REAL NOT NULL,
  CHECK (to_ts >= from_ts),
  UNIQUE (instance_id, kind, source, from_ts)
) STRICT;
CREATE INDEX ledger_time ON ledger(provider, from_ts);

CREATE TABLE reservations (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('session','job','hold','prewarm')),
  ref_id TEXT NOT NULL, instance_id TEXT REFERENCES instances(id), amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
  consumed_usd REAL NOT NULL DEFAULT 0 CHECK (consumed_usd >= 0),
  created_at REAL NOT NULL, released_at REAL, release_reason TEXT
) STRICT;
CREATE INDEX reservations_open ON reservations(released_at);

CREATE TABLE provider_accounts (
  provider TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
  blocked_reason TEXT, blocked_at REAL,
  circuit_state TEXT NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed','open','half_open')),
  circuit_opened_at REAL, error_ts_json TEXT NOT NULL DEFAULT '[]',
  last_balance_usd REAL, last_balance_at REAL
) STRICT;

CREATE TABLE provider_snapshots (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, ts REAL NOT NULL, balance_usd REAL,
  billed_usd_period REAL, period_from REAL
) STRICT;

CREATE TABLE incidents (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','high','critical')),
  ref TEXT, dedup_key TEXT UNIQUE, details_json TEXT NOT NULL DEFAULT '{}', resolved_at REAL
) STRICT;

-- вне списка §6.6, нужны логике:
CREATE TABLE host_blacklist (
  provider TEXT NOT NULL, host_id TEXT NOT NULL, until REAL NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY (provider, host_id)
) STRICT;
CREATE TABLE cost_allocations (
  instance_id TEXT NOT NULL REFERENCES instances(id), client_id TEXT NOT NULL,
  work_usd REAL NOT NULL, shared_usd REAL NOT NULL, PRIMARY KEY (instance_id, client_id)
) STRICT;
