"""POST /v1/email: validate, resolve recipients, render, send through SMTP, log.

Everything before deliver() is pure (no DB, no network) so it can be unit-tested.
"""
import html as htmllib
import re
import smtplib
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from html.parser import HTMLParser

from . import config, directory, sends
from .errors import OpsError

FIELDS = ("to", "subject", "text", "html", "template", "data")
TEMPLATES = ("notice",)
NOTICE_FIELDS = ("title", "body", "link", "link_text", "details")
ADDRESS_RE = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
MAX_RECIPIENTS = 20
ATTACHMENTS_MESSAGE = ("Attachments aren't supported. Store the file in /data and email a link to it in your app: "
                       "it stays behind login and downloads are logged")


@dataclass
class EmailRequest:
    to: list[str]
    subject: str
    text: str | None = None
    html: str | None = None
    template: str | None = None
    data: dict = field(default_factory=dict)


def parse_request(raw: object) -> EmailRequest:
    """Check the JSON body's shape. Raises OpsError(400) saying what to change."""
    if not isinstance(raw, dict):
        raise OpsError(400, "the body must be a JSON object like "
                            '{"to": ["cm"], "subject": "...", "text": "..."}')
    if "attachments" in raw:
        raise OpsError(400, ATTACHMENTS_MESSAGE)
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise OpsError(400, f"unknown field(s): {', '.join(unknown)}. Allowed fields: {', '.join(FIELDS)}")

    to = raw.get("to")
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, list) or not to or not all(isinstance(t, str) and t.strip() for t in to):
        raise OpsError(400, "to: give one or more recipients, as a string or a list of strings. "
                            'A recipient is a login ("cm") or a company address ("cm@annex.example")')
    if len(to) > MAX_RECIPIENTS:
        raise OpsError(400, f"to: {len(to)} recipients is too many; the limit is {MAX_RECIPIENTS} per email")

    subject = raw.get("subject")
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200 or "\n" in subject or "\r" in subject:
        raise OpsError(400, "subject: required, 1-200 characters on one line")

    bodies = [k for k in ("text", "html", "template") if raw.get(k) is not None]
    if len(bodies) != 1:
        found = f"got {', '.join(bodies)}" if bodies else "got none"
        raise OpsError(400, f"give exactly one of text, html or template ({found}). "
                            'For a simple message use "text"; for a ready-made layout use "template": "notice"')
    kind = bodies[0]
    if not isinstance(raw[kind], str) or not raw[kind].strip():
        raise OpsError(400, f"{kind}: must be a non-empty string")
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        raise OpsError(400, "data: must be an object, e.g. {\"title\": \"...\", \"body\": \"...\"}")
    if data and kind != "template":
        raise OpsError(400, 'data: only used with "template"; remove it, or use a template')
    req = EmailRequest(to=[t.strip() for t in to], subject=subject.strip(), data=data)
    setattr(req, kind, raw[kind])
    return req


def resolve_recipients(to: list[str], users: dict[str, dict] | None, domains: list[str],
                       allow: list[str]) -> list[str]:
    """Logins and addresses -> addresses, all or nothing.

    `users` is the directory, or None if it isn't needed (the caller loads it lazily).
    403 if any address is outside the allowed domains and allow list, 400 for an unknown login or a malformed
    address. Nothing is sent unless every recipient passes.
    """
    resolved: list[str] = []
    outside: list[str] = []
    unknown: list[str] = []
    malformed: list[str] = []
    for entry in to:
        entry = entry.strip()
        if "@" not in entry:
            user = (users or {}).get(entry.lower())
            if user and user.get("email"):
                addr = user["email"]
            else:
                unknown.append(entry)
                continue
        elif ADDRESS_RE.match(entry):
            addr = entry
        else:
            malformed.append(entry)
            continue
        addr = addr.lower()
        if addr.rsplit("@", 1)[1] in domains or addr in allow:
            if addr not in resolved:
                resolved.append(addr)
        else:
            outside.append(addr)
    if outside:
        where = ", ".join(domains) or "no domain"
        verb = "is" if len(outside) == 1 else "are"
        raise OpsError(403, f"Annex only sends to company addresses ({where}). {', '.join(outside)} {verb} outside. "
                            "If the client approves it, the platform team adds it to ANNEX_EMAIL_ALLOW. "
                            "Nothing was sent")
    if malformed:
        raise OpsError(400, f"not an email address: {', '.join(malformed)}. Use a login like \"cm\" or an address "
                            "like cm@annex.example. Nothing was sent")
    if unknown:
        known = ", ".join(sorted(users or {})) or "none"
        raise OpsError(400, f"unknown login(s): {', '.join(unknown)}. Known logins: {known}. "
                            "Or use a full company address. Nothing was sent")
    return resolved


def needs_directory(to: list[str]) -> bool:
    return any("@" not in t for t in to)


# --- rendering -------------------------------------------------------------
class _Text(HTMLParser):
    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self.href:
            self.parts.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(p.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def render_notice(data: dict) -> tuple[str, str]:
    """The built-in `notice` template: title, body and an optional link. Returns (html, text)."""
    unknown = sorted(set(data) - set(NOTICE_FIELDS))
    if unknown:
        raise OpsError(400, f"data: unknown field(s) for template notice: {', '.join(unknown)}. "
                            f"Allowed: {', '.join(NOTICE_FIELDS)}")
    for key in ("title", "body"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise OpsError(400, f'data.{key}: required for template notice, e.g. {{"title": "Report ready", '
                                '"body": "The June report is ready."}')
    details = data.get("details")
    if details is not None and not isinstance(details, str):
        raise OpsError(400, "data.details: must be a string (preformatted text such as log lines)")
    link, link_text = data.get("link"), data.get("link_text") or "Open"
    if link is not None and (not isinstance(link, str) or not re.match(r"^https?://", link)
                             or re.search(r"\s", link)):
        raise OpsError(400, "data.link: must be a single URL starting with https:// or http://")
    if not isinstance(link_text, str):
        raise OpsError(400, "data.link_text: must be a string")
    esc = htmllib.escape
    body_html = "".join(f'<p style="margin:0 0 12px">{esc(para)}</p>'
                        for para in (p.strip() for p in data["body"].split("\n\n")) if para).replace("\n", "<br>")
    button = (f'<p style="margin:20px 0 0"><a href="{esc(link, quote=True)}" style="background:#1a56db;color:#fff;'
              f'padding:10px 18px;border-radius:6px;text-decoration:none;display:inline-block">{esc(link_text)}</a></p>'
              if link else "")
    pre = (f'<pre style="margin:16px 0 0;padding:12px;background:#f3f4f6;border-radius:6px;font-size:12px;'
           f'line-height:1.4;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,Consolas,monospace">'
           f'{esc(details[:8000])}</pre>' if details and details.strip() else "")
    page = ('<!doctype html><html><body style="margin:0;padding:24px;background:#f4f5f7;'
            'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2937">'
            '<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:8px;padding:28px">'
            f'<h1 style="margin:0 0 16px;font-size:20px">{esc(data["title"])}</h1>{body_html}{button}{pre}'
            '<p style="margin:28px 0 0;font-size:12px;color:#6b7280">Sent by Kaldi Annex</p>'
            '</div></body></html>')
    text = (f"{data['title']}\n\n{data['body']}" + (f"\n\n{link_text}: {link}" if link else "")
            + (f"\n\n{details[:8000]}" if details and details.strip() else ""))
    return page, text


def render_body(req: EmailRequest) -> tuple[str, str | None]:
    """(text, html or None) for the request."""
    if req.template:
        if req.template not in TEMPLATES:
            raise OpsError(400, f"unknown template {req.template!r}. Available templates: {', '.join(TEMPLATES)}")
        html, text = render_notice(req.data)
        return text, html
    if req.html:
        return html_to_text(req.html), req.html
    return req.text or "", None


def build_message(app: str, sender: str, to: list[str], subject: str, text: str, html: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = Address(display_name="Annex" if app == "_platform" else f"{app} via Annex", addr_spec=sender)
    msg["To"] = [Address(addr_spec=a) for a in to]
    msg["Subject"] = subject
    msg["X-Annex-App"] = app
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def deliver(msg: EmailMessage) -> None:
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise OpsError(502, f"the mail server ({config.SMTP_HOST}:{config.SMTP_PORT}) didn't accept the message "
                            f"({type(e).__name__}). Try again in a minute; if it keeps failing the platform team "
                            "checks the mailpit container") from None


# --- the operation ---------------------------------------------------------
def send_email_request(app: str, raw: object, *, granted: bool = True) -> dict:
    """Everything after authentication: grant, validation, rate limit, recipients, send, log.
    Every attempt is logged, rejections included, and never the body."""
    seen_to: list[str] = []
    seen_subject: str | None = None
    if isinstance(raw, dict):
        t = raw.get("to")
        seen_to = [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)] if isinstance(t, list) else []
        seen_subject = raw.get("subject") if isinstance(raw.get("subject"), str) else None
    try:
        if not granted:
            raise OpsError(403, "this app may not send email: add notify: {email: true} to annex.yaml and "
                                "deploy again")
        req = parse_request(raw)
        sends.check_rate(app, "email", config.EMAIL_PER_HOUR, "emails")
        users = directory.users() if needs_directory(req.to) else None
        to = resolve_recipients(req.to, users, config.EMAIL_DOMAINS, config.EMAIL_ALLOW)
        seen_to = to
        text, html = render_body(req)
        deliver(build_message(app, config.EMAIL_FROM, to, req.subject, text, html))
    except OpsError as e:
        sends.log_failure(app, "email", seen_to[:MAX_RECIPIENTS], seen_subject, e)
        raise
    return {"id": sends.log_send(app, "email", to, req.subject, "sent"), "to": to}


def send_email(app: str, to, subject: str, *, text: str | None = None, html: str | None = None,
               template: str | None = None, data: dict | None = None) -> dict:
    """For code inside annex-ops (M1.3's alerts call this with app="_platform"): no token or grant check,
    but recipients are still validated and the send is logged."""
    raw = {"to": to, "subject": subject, "text": text, "html": html, "template": template, "data": data}
    return send_email_request(app, {k: v for k, v in raw.items() if v is not None})
