"""
One-time script: create Supabase tables and seed tenant rows.

This project's Supabase DB is IPv6-only. If this machine lacks IPv6 connectivity
the script prints the SQL and a link to run it in the Supabase SQL editor instead.

Run: python setup_supabase.py
"""
import os
import sys

SCHEMA_SQL = """
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
  flagged_for_review BOOLEAN
);

INSERT INTO tenants (name, display_name) VALUES
  ('migratehq', 'MigrateHQ'),
  ('olist', 'Olist')
ON CONFLICT (name) DO NOTHING;
"""

SUPABASE_SQL_EDITOR = "https://app.supabase.com/project/xcmqetbsaqclnyxiebzp/sql/new"


def try_psycopg2():
    try:
        import psycopg2
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

        conn = psycopg2.connect(
            host=os.environ["SUPABASE_DB_HOST"],
            port=int(os.environ["SUPABASE_DB_PORT"]),
            dbname=os.environ["SUPABASE_DB_NAME"],
            user=os.environ["SUPABASE_DB_USER"],
            password=os.environ["SUPABASE_DB_PASSWORD"],
            sslmode="require",
            connect_timeout=10,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)


def main():
    print("Attempting direct DB connection...")
    ok, err = try_psycopg2()
    if ok:
        print("Supabase schema created successfully.")
        print("Tables: tenants, clients, mapping_runs, mapping_results")
        return

    print(f"Direct connection failed ({err})")
    print()
    print("=" * 70)
    print("ACTION REQUIRED: run the following SQL in the Supabase SQL editor")
    print(f"URL: {SUPABASE_SQL_EDITOR}")
    print("=" * 70)
    print(SCHEMA_SQL)
    print("=" * 70)

    # Write to a file for convenience
    sql_file = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(sql_file, "w") as f:
        f.write(SCHEMA_SQL)
    print(f"SQL also written to: {sql_file}")


if __name__ == "__main__":
    main()
