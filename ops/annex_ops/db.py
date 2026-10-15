"""Postgres access: one short-lived connection per request is plenty at this volume."""
import psycopg

from . import config
from .errors import OpsError


def connect() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise OpsError(503, "annex-ops has no database configured (OPS_DATABASE_URL). "
                            "The platform team runs scripts/bootstrap.sh and scripts/push.sh")
    try:
        return psycopg.connect(config.DATABASE_URL, autocommit=True, connect_timeout=5)
    except psycopg.Error:
        raise OpsError(503, "annex-ops can't reach its database right now. Try again in a minute; if it "
                            "keeps failing the platform team checks appdata-db") from None
