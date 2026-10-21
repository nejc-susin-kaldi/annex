-- What the client grants Annex: read the arcade schema, nothing else.
GRANT USAGE ON SCHEMA arcade TO annex_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA arcade TO annex_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA arcade GRANT SELECT ON TABLES TO annex_reader;
ANALYZE;
