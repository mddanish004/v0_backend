-- Roll back migrations/bul/add_match_outcome.sql.
--
-- Destroys walkover reasons and outcome types. Any WALKOVER match becomes a
-- regular match with winner_team_id/is_final/status left as they are.
--
--   mysql -u <user> -p <database> < migrations/bul/add_match_outcome_down.sql

ALTER TABLE `match`
DROP COLUMN `outcome_reason`,
DROP COLUMN `outcome_type`;
