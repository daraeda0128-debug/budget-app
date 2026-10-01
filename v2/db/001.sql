BEGIN;
CREATE TABLE users (id bigserial PRIMARY KEY, username text UNIQUE NOT NULL, password_hash text NOT NULL, disabled boolean NOT NULL DEFAULT false);
CREATE TABLE sessions (token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id), csrf text NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE login_attempts (username text PRIMARY KEY, failures int NOT NULL, retry_at timestamptz NOT NULL);
CREATE TABLE passkeys (credential_id bytea PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id), public_key bytea NOT NULL, sign_count bigint NOT NULL DEFAULT 0, transports jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE import_batches (id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), report jsonb NOT NULL);
CREATE TABLE transactions (
 id text PRIMARY KEY, source_month char(7) NOT NULL, occurred_on date NOT NULL,
 name text NOT NULL, category text NOT NULL, amount bigint NOT NULL CHECK(amount > 0),
 direction text NOT NULL CHECK(direction IN ('income','expense')),
 owner text NOT NULL CHECK(owner IN ('j','m','b')),
 payment_method text CHECK(payment_method IN ('card','cash')),
 memo text NOT NULL DEFAULT '', legacy jsonb NOT NULL,
 batch_id text REFERENCES import_batches(id), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON transactions(occurred_on);
CREATE TABLE legacy_documents (path text PRIMARY KEY, value jsonb NOT NULL, batch_id text NOT NULL REFERENCES import_batches(id));
-- Preserve monthly fixed snapshots rather than inferring recurrence from copies.
CREATE TABLE fixed_snapshots (month char(7) PRIMARY KEY, items jsonb NOT NULL, batch_id text REFERENCES import_batches(id));
CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE carry_anchors (month char(7) PRIMARY KEY, amount bigint NOT NULL);
CREATE TABLE simulation_events (id text PRIMARY KEY, value jsonb NOT NULL);
-- Planned normalized rules: materialization must use a UNIQUE rule/month occurrence.
CREATE TABLE recurring_rules (id bigserial PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('salary','fixed')), owner text NOT NULL, amount bigint NOT NULL CHECK(amount>=0), day int CHECK(day BETWEEN 1 AND 31), start_month char(7), end_month char(7), payment_method text, metadata jsonb NOT NULL DEFAULT '{}');
CREATE TABLE rule_occurrences (rule_id bigint REFERENCES recurring_rules(id), month char(7), transaction_id text REFERENCES transactions(id), PRIMARY KEY(rule_id,month));
CREATE TABLE settlements (id bigserial PRIMARY KEY, advance_id text REFERENCES transactions(id), repayment_id text REFERENCES transactions(id), amount bigint NOT NULL CHECK(amount>0));
COMMIT;
