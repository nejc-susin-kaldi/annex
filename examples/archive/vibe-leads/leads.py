"""The lead form: its choices, validation, and the summary sent to the assignee."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

NET_WORTH = [
    "Under €250k",
    "€250k – €1M",
    "€1M – €5M",
    "€5M – €20M",
    "Over €20M",
    "Not sure yet",
]

SERVICES = [
    "Portfolio management",
    "Investment advisory",
    "Financial & retirement planning",
    "Tax planning",
    "Estate & succession planning",
    "Pension solutions",
    "Insurance",
    "Corporate / business services",
]

CHANNELS = {"slack": "Slack", "email": "Email"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def default_quote_due(today: date | None = None, business_days: int = 5) -> date:
    d = today or date.today()
    while business_days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            business_days -= 1
    return d


@dataclass
class Lead:
    client_name: str
    email: str
    phone: str
    net_worth: str
    services: list[str]
    note: str
    assignee_id: int
    channel: str
    quote_due: date


def parse(form, *, require_channel: bool = True) -> tuple[Lead | None, dict[str, str]]:
    """Validate the submitted form. Returns the lead, or None and an error per field.
    In test mode no channel can be picked, so `require_channel` is off."""
    get = lambda k: (form.get(k) or "").strip()  # noqa: E731
    errors: dict[str, str] = {}

    if not get("client_name"):
        errors["client_name"] = "Who is the potential client?"
    email, phone = get("email"), get("phone")
    if not email and not phone:
        errors["email"] = "Give at least an email or a phone number."
    elif email and not EMAIL_RE.match(email):
        errors["email"] = "That doesn't look like an email address."
    if get("net_worth") not in NET_WORTH:
        errors["net_worth"] = "Pick an approximate net worth (or “Not sure yet”)."
    services = [s for s in form.getlist("services") if s in SERVICES]
    if not services:
        errors["services"] = "Pick at least one service they might be interested in."
    try:
        assignee_id = int(get("assignee_id"))
    except ValueError:
        assignee_id = 0
        errors["assignee_id"] = "Choose who should follow up."
    if require_channel and get("channel") not in CHANNELS:
        errors["channel"] = "Choose how to notify them."

    if errors:
        return None, errors
    return Lead(
        client_name=get("client_name"),
        email=email,
        phone=phone,
        net_worth=get("net_worth"),
        services=services,
        note=get("note"),
        assignee_id=assignee_id,
        channel=get("channel"),
        quote_due=default_quote_due(),
    ), {}


def fmt_date(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b %Y}"


def summary_fields(lead: Lead) -> list[tuple[str, str]]:
    """The lead as (label, value) rows, skipping anything left empty."""
    rows = [
        ("Client", lead.client_name),
        ("Approx. net worth", lead.net_worth),
        ("Interested in", ", ".join(lead.services)),
        ("Contact", " · ".join(x for x in (lead.email, lead.phone) if x)),
        ("Note", lead.note),
    ]
    return [(k, v) for k, v in rows if v]
