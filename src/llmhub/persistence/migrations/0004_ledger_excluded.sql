-- T-372 D1 (critic F14, customer Q5): ledger rows are excluded, never deleted.
-- excluded_at: when an operator excluded the row (`ledger-exclude`); every ledger sum reads
-- only rows with excluded_at IS NULL. excluded_reason: short tag (e.g. 't372-mock').
ALTER TABLE ledger ADD COLUMN excluded_at REAL;
ALTER TABLE ledger ADD COLUMN excluded_reason TEXT;
