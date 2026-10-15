"""Bearer token -> sha256 -> the app's grant row. Tokens are only ever stored hashed."""
import hashlib
from dataclasses import dataclass, field

from . import db
from .errors import OpsError


@dataclass
class Grant:
    app: str
    email: bool = False
    slack_channels: list[str] = field(default_factory=list)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def authenticate(header: str | None) -> Grant:
    token = bearer_token(header)
    row = None
    if token:
        with db.connect() as conn:
            row = conn.execute("SELECT app, email, slack_channels FROM annex_ops.app_grants "
                               "WHERE token_sha256 = %s", (token_hash(token),)).fetchone()
    if not row:
        raise OpsError(401, "missing or unknown ANNEX_OPS_TOKEN. Declare notify: in annex.yaml and deploy "
                            "again; Annex injects the token")
    return Grant(app=row[0], email=row[1], slack_channels=list(row[2]))
