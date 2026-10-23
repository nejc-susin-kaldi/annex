"""vibe-leads: capture a new client lead and hand it to a teammate to follow up with a quote."""
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import db
import leads
import notify
import pages
from annex_runtime import current_user

app = FastAPI(title="vibe-leads")


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _channels_ready() -> dict[str, bool]:
    return {c: notify.channel_ready(c) for c in leads.CHANNELS}


# Plain `def` endpoints run in a thread pool, so blocking DB calls are fine.
@app.get("/", response_class=HTMLResponse)
def lead_form(request: Request, sent: int = 0, via: str = ""):
    user = current_user(request.headers)
    members = db.list_members()
    message = ""
    if sent and via in leads.CHANNELS:
        who = next((m.name for m in members if m.id == sent), "your teammate")
        message = f"Lead sent to {who} via {leads.CHANNELS[via]}. Fill in the form again for the next one."
    return pages.lead_form(user, members, {}, {}, channels_ready=_channels_ready(), sent=message)


# Async so we can read multi-value checkboxes from the raw form; blocking work goes to the thread pool.
@app.post("/", response_class=HTMLResponse)
async def submit_lead(request: Request):
    user = current_user(request.headers)
    form = await request.form()
    values = {k: v for k, v in form.items()}
    values["services"] = form.getlist("services")
    members = await run_in_threadpool(db.list_members)
    ready = _channels_ready()

    def again(errors: dict, failure: str = ""):
        return HTMLResponse(pages.lead_form(user, members, values, errors, channels_ready=ready,
                                            failure=failure), status_code=400 if errors else 502)

    # Test mode: neither Slack nor email is set up, so show what would be sent instead of sending.
    test_mode = not any(ready.values())
    lead, errors = leads.parse(form, require_channel=not test_mode)
    assignee = next((m for m in members if lead and m.id == lead.assignee_id), None)
    if lead and not assignee:
        errors["assignee_id"] = "That person is no longer on the team. Choose someone else."
    if lead and not test_mode and not ready[lead.channel]:
        errors["channel"] = f"{leads.CHANNELS[lead.channel]} isn't set up yet. Choose the other option."
    if errors:
        return again(errors)

    if test_mode:
        print(f"lead previewed (test mode): assignee={assignee.id} by={user.login}", flush=True)
        preview = (f"{assignee.name} <{assignee.email}>", notify.subject(lead),
                   notify.message_text(lead, assignee, user))
        return HTMLResponse(pages.lead_form(user, members, values, {}, channels_ready=ready,
                                            preview=preview))

    try:
        await run_in_threadpool(notify.send, lead.channel, lead, assignee, user)
    except notify.NotifyError as e:
        print(f"notify failed: channel={lead.channel} assignee={assignee.id} by={user.login}: {e}",
              flush=True)
        return again({}, failure=f"Couldn't send via {leads.CHANNELS[lead.channel]}: {e}.")

    print(f"lead sent: channel={lead.channel} assignee={assignee.id} by={user.login}", flush=True)
    return RedirectResponse(f"./?sent={assignee.id}&via={lead.channel}", status_code=303)


@app.get("/team", response_class=HTMLResponse)
def team(request: Request, notice: str = ""):
    user = current_user(request.headers)
    messages = {"added": "Added to the team.", "removed": "Removed from the team."}
    return pages.team_page(user, db.list_members(), {}, {}, notice=messages.get(notice, ""))


@app.post("/team", response_class=HTMLResponse)
async def add_member(request: Request):
    user = current_user(request.headers)
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k in ("name", "email", "slack_id")}
    errors = {}
    if not values["name"]:
        errors["name"] = "Add their name."
    if not leads.EMAIL_RE.match(values["email"]):
        errors["email"] = "Add a valid email address."
    slack_id = values["slack_id"].upper()
    if slack_id and not (slack_id[0] in "UW" and slack_id.isalnum() and len(slack_id) >= 9):
        errors["slack_id"] = "A Slack member ID looks like U04ABCD1234 (not a @handle)."
    if not errors:
        try:
            await run_in_threadpool(db.add_member, values["name"], values["email"].lower(), slack_id,
                                    user.name)
            return RedirectResponse("team?notice=added", status_code=303)
        except db.DuplicateEmail:
            errors["email"] = "Someone with this email is already on the team."
    members = await run_in_threadpool(db.list_members)
    return HTMLResponse(pages.team_page(user, members, values, errors), status_code=400)


@app.post("/team/{member_id}/remove")
def remove_member(member_id: int):
    db.remove_member(member_id)
    return RedirectResponse("../../team?notice=removed", status_code=303)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True,
                forwarded_allow_ips="*")
