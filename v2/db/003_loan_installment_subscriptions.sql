BEGIN;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS loan_principal bigint;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS execution_date date;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS term_months int;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS annual_rate_bps int;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS repayment_method text;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS payment_day int;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS grace_months int NOT NULL DEFAULT 0;
ALTER TABLE automatic_payments ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'autopay';
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='financial_accounts_repayment_method_check') THEN
  ALTER TABLE financial_accounts ADD CONSTRAINT financial_accounts_repayment_method_check CHECK(repayment_method IS NULL OR repayment_method IN ('equal_payment','equal_principal','bullet','grace_equal_payment'));
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='automatic_payments_kind_check') THEN
  ALTER TABLE automatic_payments ADD CONSTRAINT automatic_payments_kind_check CHECK(kind IN ('autopay','subscription'));
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS card_installments (
 id text PRIMARY KEY,
 card_account_id text NOT NULL REFERENCES financial_accounts(id) ON DELETE CASCADE,
 name text NOT NULL,
 purchase_date date NOT NULL,
 total_amount bigint NOT NULL CHECK(total_amount>0),
 installment_count int NOT NULL CHECK(installment_count BETWEEN 2 AND 120),
 first_billing_month char(7) NOT NULL,
 annual_rate_bps int NOT NULL DEFAULT 0 CHECK(annual_rate_bps BETWEEN 0 AND 100000),
 notes text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;

