-- PostgREST runs annex_gateway.log_request() before every request, so each
-- gateway call is logged with the app's role (see product-db container logs).
CREATE SCHEMA IF NOT EXISTS annex_gateway;

CREATE OR REPLACE FUNCTION annex_gateway.log_request() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  RAISE LOG 'annex-gateway role=% method=% path=%',
    current_user,
    current_setting('request.method', true),
    current_setting('request.path', true);
END
$$;

GRANT USAGE ON SCHEMA annex_gateway TO PUBLIC;
GRANT EXECUTE ON FUNCTION annex_gateway.log_request() TO PUBLIC;
