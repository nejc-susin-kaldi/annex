-- 005 (V3, D103): the CLI that made each release.

-- A release line (releases.jsonl) now carries `cli`: {commit, dirty}, the commit of the checkout the CLI ran from when it
-- deployed. annex-ops copies it here so the console's Releases and Config pages can show it. A new nullable column;
-- older rows stay NULL (they were made before the CLI reported its commit). annex_console already has table-level SELECT.
ALTER TABLE annex_ops.releases ADD COLUMN IF NOT EXISTS cli jsonb;   -- {commit, dirty}
