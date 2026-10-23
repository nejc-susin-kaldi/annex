"""Send a lead to its assignee via a Slack channel webhook or by email."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from annex_runtime import User
from db import Member
from leads import Lead, fmt_date, summary_fields


class NotifyError(Exception):
    pass


def slack_ready() -> bool:
    return bool(os.environ.get("SLACK_WEBHOOK_URL"))


def email_ready() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def channel_ready(channel: str) -> bool:
    return {"slack": slack_ready, "email": email_ready}[channel]()


def send(channel: str, lead: Lead, to: Member, by: User) -> None:
    if channel == "slack":
        _send_slack(lead, to, by)
    else:
        _send_email(lead, to, by)


def _first_name(name: str) -> str:
    return name.split()[0] if name.strip() else name


def _slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _send_slack(lead: Lead, to: Member, by: User) -> None:
    e = _slack_escape
    mention = f"<@{to.slack_id}>" if to.slack_id else f"*{e(to.name)}*"
    due = fmt_date(lead.quote_due)
    rows = "\n".join(f"*{e(k)}:* {e(v)}" for k, v in summary_fields(lead))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"{mention}, you have a new lead: *{e(lead.client_name)}*\n"
            f"Assigned by {e(by.name)} · quote due *{due}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Next step: contact {e(_first_name(lead.client_name))} and send them a quote by {due}."}]},
    ]
    payload = {"text": f"New lead for {to.name}: {lead.client_name} (quote due {due})", "blocks": blocks}
    req = Request(os.environ["SLACK_WEBHOOK_URL"], data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as r:
            r.read()
    except HTTPError as err:
        raise NotifyError(f"Slack returned {err.code}: {err.read().decode(errors='replace')[:200]}") from None
    except URLError as err:
        raise NotifyError(f"couldn't reach Slack ({err.reason})") from None


def subject(lead: Lead) -> str:
    return f"New lead: {lead.client_name} (quote due {fmt_date(lead.quote_due)})"


def message_text(lead: Lead, to: Member, by: User) -> str:
    """The plain-text message: the email body, and what test mode previews."""
    rows = "\n".join(f"{k}: {v}" for k, v in summary_fields(lead))
    return (
        f"Hi {_first_name(to.name)},\n\n"
        f"{by.name} has assigned you a new lead: {lead.client_name}.\n\n"
        f"{rows}\n\n"
        f"Next step: contact {_first_name(lead.client_name)} and send them a quote by "
        f"{fmt_date(lead.quote_due)}.\n\n"
        f"Sent from vibe-leads\n"
    )


def _send_email(lead: Lead, to: Member, by: User) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject(lead)
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = f"{to.name} <{to.email}>"
    if by.email:
        msg["Reply-To"] = f"{by.name} <{by.email}>"
    msg.set_content(message_text(lead, to, by))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    username = os.environ.get("SMTP_USERNAME", "")
    context = ssl.create_default_context()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=context)
            elif username:
                smtp.close()
                raise NotifyError("the mail server doesn't offer STARTTLS, so the password wasn't sent")
        with smtp:
            if username:
                smtp.login(username, os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        raise NotifyError(f"couldn't send the email ({err})") from None
