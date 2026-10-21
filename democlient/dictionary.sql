-- The Goat Arcade data dictionary as Markdown, from the comments on the gateway views (api_views_arcade.sql).
-- Run on product-db by scripts/democlient.sh dictionary, which writes docs/goat-arcade/data-dictionary.md.
SELECT string_agg(section, E'\n' ORDER BY oid)
FROM (
  SELECT c.oid,
         format(E'## `%s`\n%s\n\n| Column | Type | Meaning |\n|---|---|---|\n%s\n', c.relname,
                upper(substr(obj_description(c.oid, 'pg_class'), 14, 1)) || substr(obj_description(c.oid, 'pg_class'), 15),
                string_agg(format('| `%s` | %s | %s |', a.attname, format_type(a.atttypid, a.atttypmod),
                                  replace(coalesce(col_description(c.oid, a.attnum), ''), '|', '\|')), E'\n' ORDER BY a.attnum)) AS section
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'annex_api'
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  WHERE c.relkind = 'v' AND obj_description(c.oid, 'pg_class') LIKE 'Goat Arcade:%'
  GROUP BY c.oid, c.relname
) s;
