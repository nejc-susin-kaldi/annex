# party-bookings

Birthday-party requests from Goat Arcade's public website, and the staff page that answers them. The website form
posts each request to Annex's public webhook URL; staff see it behind the login, confirm it (venue and slot), decline
it with a reason or mark it as contacted, and the venue's manager gets an email at each step that matters.

Owner `maja` (Maja Horvat, venue manager, Metelkova LJ1). The one Goat Arcade tool that receives calls from outside.
All customers in the demo are fictional (`@example.com`).

- **The webhook** `website` (`verify: shared-secret`, secret `BOOKING_HOOK_SECRET`): the website sends
  `POST https://hooks.<domain>/h/party-bookings/website` with the header `X-Annex-Hook-Secret` and a JSON body:
  `name`, `email`, `phone`, `preferred_venue` (a venue code), `date` (YYYY-MM-DD), `time` (HH:MM), `kids` (count),
  `age` (the birthday child's), `note`. Annex checks the secret, stores the event and forwards it to
  `POST /hooks/website`, which calls `hook_event()` first (401 otherwise), answers at once and **dedupes by the event
  id**: a redelivery or a replay of the same event is ignored (`duplicate event ignored` in the log). Every problem
  with a request (no name, neither email nor phone, an unknown venue, a date in the past or over a year ahead, kids
  not 1-40, age not 1-18...) is listed; such a request is stored as **rejected** with the reasons and still answered
  2xx, so Annex doesn't retry what can never succeed. Staff see rejected ones too, with the fields as sent.
- **The staff page** (`/`): requests with new ones first, then contacted, confirmed, declined and rejected, each
  with its preferred venue and date; counts per status; a filter by venue (preferred or booked) and status.
  A request's page (`/r/<id>`) has the details, the customer's email as a `mailto:` link and phone as `tel:`, the
  actions, and its history: who did what and when (the login from `Remote-User`; `website` and `party-bookings` for
  what the app did itself, such as emails).
- **Opening hours** come from the gateway view `venues` (also the venue names). A requested or chosen slot of
  `PARTY_HOURS` (default 2) that falls outside the venue's hours that day (including spans past midnight) is flagged
  on the page and in the manager's email; confirming such a slot asks for "Confirm anyway".
- **Storage:** the app's own Postgres: `bookings` and `booking_history`. Forms are POSTs that pass the template's
  CSRF check.

## Email: staff only, never the customer
Annex only sends email to logins and company addresses, so **the app never emails the customer**. This is on purpose
and fine for a small team:
- on a new request, the manager of the preferred venue gets "Party request: ..." with the details and a link;
- on a confirmation, the manager of the booked venue (and whoever confirmed it) gets "Confirmed: party for ..." with a
  ready-to-forward message for the customer;
- for anything else staff reply by hand from the `mailto:` link on the request's page.

Who manages which venue is the setting `VENUE_MANAGERS` in `annex.yaml`: `venue_code=login` pairs, with `*` for every
other venue (now `LJ1=maja,*=maja`; add e.g. `MB=boris` when Maribor wants its own requests).

## Sending a test booking
`scripts/demo-booking.sh` (in the repo's `scripts/`) sends one realistic, correctly signed request the way the
website does, to the public hook URL. The secret is read and used only on the host where it lives (the VM, or the
local stack's secrets directory with `ANNEX_ENV=local`); it is never copied to the Mac's shell or printed.
`--venue LJ1`, `--date 2026-11-07`, `--time 15:00`, `--kids 8`, `--age 7`, `--child Luka`, and `--bad` for a request the
app must reject. `annex hooks party-bookings` lists the events.

Tests for the pure part (validation, managers, opening hours): `uv run --python 3.13 --with pytest pytest tests -q`
in this folder.

## How it was built
Maja's request, as typed into Claude Code:

> Our website has a form for birthday parties and right now those requests land in a shared inbox where nobody owns
> them. Build party-bookings on Annex, owner maja: the website form posts each request to it (name, email, phone,
> which venue they'd like, date, time, how many kids and how old the birthday child is, a note). Give us one page with
> the new ones on top where we can confirm a booking (pick the venue and the time), decline it with a reason, or mark
> that we called them, and see who did what. Filter by venue and status. Email me when a request comes in for
> Metelkova, and when we confirm one send me a summary I can forward to the parents. Warn us if the time is outside
> the venue's opening hours. Junk from the form shouldn't break anything.

**Review pass** (after the first deploy):
- The website's secret: `scripts/demo-booking.sh` first passed the CA to curl through a pipe, which curl on macOS
  can't read; it now writes the (public) CA to a temporary file under the data directory, removed on exit.
- The opening-hours warning read "on saturdays" (Python's `capitalize()` lowered the rest); fixed. The confirm button
  on an already confirmed request now says "Save the new slot".
- Checked a redelivery of an already stored event (same event id, attempt 2): answered `{"duplicate": true}`, nothing
  stored or emailed twice; a call without Annex's token gets 401.
