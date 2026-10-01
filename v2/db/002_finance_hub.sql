BEGIN;
CREATE TABLE IF NOT EXISTS financial_accounts (
 id text PRIMARY KEY,
 kind text NOT NULL CHECK(kind IN ('bank','loan','investment','card')),
 name text NOT NULL,
 institution text NOT NULL DEFAULT '',
 owner text NOT NULL CHECK(owner IN ('j','m','b')),
 last_four text NOT NULL DEFAULT '',
 balance bigint,
 credit_limit bigint,
 billing_day int CHECK(billing_day BETWEEN 1 AND 31),
 notes text NOT NULL DEFAULT '',
 benefits jsonb NOT NULL DEFAULT '[]'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS automatic_payments (
 id text PRIMARY KEY,
 name text NOT NULL,
 amount bigint NOT NULL DEFAULT 0 CHECK(amount>=0),
 cadence text NOT NULL CHECK(cadence IN ('monthly','quarterly','yearly','weekly')),
 debit_day int CHECK(debit_day BETWEEN 1 AND 31),
 category text NOT NULL DEFAULT '기타',
 source_account_id text REFERENCES financial_accounts(id) ON DELETE SET NULL,
 related_card_id text REFERENCES financial_accounts(id) ON DELETE SET NULL,
 notes text NOT NULL DEFAULT '',
 active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_account_id text REFERENCES financial_accounts(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS automatic_payments_active_idx ON automatic_payments(active);
COMMIT;

