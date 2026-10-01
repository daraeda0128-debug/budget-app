BEGIN;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS principal_defer_start date;
ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS principal_defer_months int NOT NULL DEFAULT 0;
ALTER TABLE financial_accounts DROP CONSTRAINT IF EXISTS financial_accounts_repayment_method_check;
ALTER TABLE financial_accounts ADD CONSTRAINT financial_accounts_repayment_method_check
  CHECK(repayment_method IS NULL OR repayment_method IN ('equal_payment','equal_principal','bullet','grace_equal_payment','principal_defer'));
COMMIT;

