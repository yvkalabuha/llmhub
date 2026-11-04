-- T-368 review fix round 1 (R1-5, F-R2-1): create times that are never cleared.
-- first_create_called_at: first create call of the row; last_create_called_at: latest call;
-- create_unknown = 1 once any create of the row had an unknown outcome (zero is then confirmed
-- not before first_create_called_at + N * create_visibility_grace_s).
ALTER TABLE instances ADD COLUMN first_create_called_at REAL;
ALTER TABLE instances ADD COLUMN last_create_called_at REAL;
ALTER TABLE instances ADD COLUMN create_unknown INTEGER NOT NULL DEFAULT 0;
