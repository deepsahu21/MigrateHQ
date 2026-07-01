
CREATE TABLE IF NOT EXISTS tenants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS clients (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID REFERENCES tenants(id),
  source_dataset TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mapping_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID REFERENCES clients(id),
  run_id TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  total_columns INT,
  l1_count INT,
  l2_count INT,
  fallback_count INT,
  accuracy_pct FLOAT,
  status TEXT
);

CREATE TABLE IF NOT EXISTS mapping_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id TEXT REFERENCES mapping_runs(run_id),
  source_column TEXT,
  target_column TEXT,
  confidence FLOAT,
  layer TEXT,
  correct BOOLEAN,
  flagged_for_review BOOLEAN,
  source_samples JSONB,
  target_samples JSONB,
  explanation TEXT,
  status TEXT NOT NULL DEFAULT 'pending_review'
    CHECK (status IN ('pending_review', 'approved', 'rejected'))
);

-- Apply missing columns to existing deployments (idempotent)
ALTER TABLE mapping_results ADD COLUMN IF NOT EXISTS source_samples JSONB;
ALTER TABLE mapping_results ADD COLUMN IF NOT EXISTS target_samples JSONB;
ALTER TABLE mapping_results ADD COLUMN IF NOT EXISTS explanation TEXT;
ALTER TABLE mapping_results ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending_review'
  CHECK (status IN ('pending_review', 'approved', 'rejected'));

INSERT INTO tenants (name, display_name) VALUES
  ('migratehq', 'MigrateHQ'),
  ('olist', 'Olist')
ON CONFLICT (name) DO NOTHING;
