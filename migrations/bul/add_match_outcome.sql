-- Match outcomes: HOW a match was decided.
--
-- Existing rows are played matches, so the default backfills them to REGULAR.
-- outcome_reason is only used by outcomes that record why (e.g. WALKOVER).
--
-- Apply once, in order, to any database created before this change:
--   mysql -u <user> -p <database> < migrations/bul/add_match_outcome.sql
--
-- New databases get these columns from db.create_all() (models.Match).

ALTER TABLE `match`
ADD COLUMN `outcome_type` VARCHAR(20) NOT NULL DEFAULT 'REGULAR',
ADD COLUMN `outcome_reason` VARCHAR(255) NULL;
