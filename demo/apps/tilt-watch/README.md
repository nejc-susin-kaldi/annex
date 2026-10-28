# tilt-watch

Which pinball cabinets tilt far more than their model does? For the last 30 days in the data, each pinball cabinet's
tilt rate (games that ended in a tilt, out of all its games) next to its model's average, with the ratio. Anything
over 3x is flagged, worst first; below, every cabinet ranked.

Owner `sam`. Goat Arcade data, all synthetic. Team: Technical (`platform/config/teams.json`). **This is the demo's live build**
(`docs/demo.md`): on stage Claude Code builds it from an empty folder; this copy is the fallback, deployed by
`scripts/reset-demo.sh --keep tilt-watch`. On the data as seeded it flags the **Cyber Samurai in Graz** (Goat Arcade
Lend, machine #108) at about 5x the other Cyber Samurai cabinets: one of the stories planted in the data (D78, story 6).

- **Views:** `daily_plays` (games and tilts per machine per day, the 30 days to the latest day in the data),
  `machine_floor` (what stands where now, the model, out of order or an open ticket), `machine_models` (the model of
  a cabinet that's in storage, the maker and year). Reads are cached for 5 minutes. No data of its own.
- **Model average** is the tilt rate of the model's **other** cabinets (games-weighted), not of all of them: a
  cabinet must not be part of its own yardstick (see the review pass). A model with one cabinet has nothing to
  compare with ("only one").
- **Flagged:** over `FLAG_FACTOR` (3) times the model average, with at least `MIN_TILTS` (5) tilts in the 30 days and
  at least 50 games on the other cabinets; fewer is chance. Both are `env:` in `annex.yaml`.
- **Time** is relative to the latest day in `daily_plays`, never today, so it works after a reseed. Machines move:
  a cabinet's tilts count wherever it stood; "Stands at" is where it is now.

Tests for the pure part (`tilts.py`): `uv run --python 3.13 --with pytest pytest tests -q` in this folder.

## How it was built
Sam's request, as typed into Claude Code (the demo's prompt, `docs/demo.md`):

> Build tilt-watch on Annex, owner sam: for the last 30 days, each pinball machine's tilt rate next to its model's
> average. Flag anything over 3x, worst first. Deploy it and give me the URL.

Claude Code read the contract and `docs/goat-arcade/agent-brief.md`, started from `annex init python-web tilt-watch
--owner sam`, and read `daily_plays` (it has `tilts` and `games` per machine per day), `machine_floor` and
`machine_models`.

**Review pass** (after the first deploy):
- The first version took the model's average over **all** its cabinets and flagged nothing: the Graz Cyber Samurai
  tilts 13.2% of its games and pulls its own model's average up to 5.0%, so it showed 2.6x. Against the other three
  Cyber Samurai cabinets (2.5%) it is 5.2x. The average is now the model's other cabinets, and the page says so.
- Small counts made noise: a cabinet with 2 tilts in 20 games looked like 4x. A flag now needs 5 tilts in the window.
- A cabinet in storage has no row in `machine_floor`; its model now comes from `machine_models` by title, and the
  page says "not on a floor now" with where it last stood.
- The venue column shows the venue's name and city, not only its code.

What it flagged on the local lane (data to 21 Oct 2026): Cyber Samurai #108, Graz, 13.2% against 2.5% (5.2x); Punk
Planet #90, Zagreb, 8.3% against 2.0% (4.1x); Mummy's Revenge #5, Metelkova, 5.1% against 1.6% (3.2x). The two
after the first are within chance over 30 days (the planted story is the first); over the whole 180 days only the
Cyber Samurai stands out.
