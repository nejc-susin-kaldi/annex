# venue-pulse

How is my venue doing? The last 7 days in the data against the 7 before: games and revenue with the change, a
games-per-day chart (14 days), the biggest risers and fallers per machine, idle machines with their service state,
and rotation candidates: models on this floor that earn clearly more per cabinet per day at another venue.

Owner `maja` (Maja Horvat, venue manager, Metelkova LJ1). Goat Arcade data, all synthetic.

- **The venue follows the user.** A venue picked on the page is saved per login in the app's own Postgres
  (`venue_choice`); without one, `HOME_VENUES` in `annex.yaml` gives the default (`maja=LJ1,boris=MB,lena=GZ`);
  anyone else picks one first. `/?venue=ZG` shows a venue without saving it. Everyone signed in may look at any venue.
- **Time** is relative to the latest day in `daily_plays`, never today. The page shows the dates it compares.
- **Views:** `venues` (names, picker), `machine_floor` (what stands where now, games_7d, tickets), `daily_plays`
  (the last 30 days, all venues: weeks, chart, rotation). Reads are cached for 5 minutes.
- **Rotation rate:** revenue per cabinet per day by model and venue over 30 days. Machines move, so each cabinet's
  days count where it stood (see `pulse.model_rates`); a candidate earns at least 1.5x and EUR 5/day more elsewhere.
  A candidate whose cabinet here is out of order is marked "fix first".
- `IDLE_GAMES_7D` (default 20): fewer games than this in 7 days is idle.

Tests for the pure part: `uv run --python 3.13 --with pytest pytest tests -q` in this folder.

## How it was built
Maja's request, as typed into Claude Code:

> Make me a page on Annex, owner maja, that tells me how Metelkova is doing this week compared to last week: games,
> money and the change, and per machine what it earned this week and last, with the biggest jumps up and down.
> Show me which machines on my floor hardly get played and whether they're broken or have a ticket open. And tell
> me if a game would earn more somewhere else, like "Mountain Goat Mayhem makes €41 a day in Zagreb and €18 here".
> It should open on my venue when I log in, but let me switch, and remember what I picked. Sam and the others should
> be able to use it for their venues too. One small chart of games per day for the last two weeks would be nice.

**Review pass** (after the first deploy):
- Rotation candidates: a model that earns little here because its only cabinet is out of order was suggested for
  a move (Haunted Lighthouse in Maribor). Such rows are now marked "out of order here: fix first" or "open ticket".
- When nothing qualifies (Metelkova is the busiest floor), the section says how many models earn the most right
  here instead of a bare "none".
- Fixed the Graz code in `HOME_VENUES` (`GZ`, not `GR`) and labelled the per-machine games column.
