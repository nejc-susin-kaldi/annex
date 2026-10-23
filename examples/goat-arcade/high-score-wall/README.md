# high-score-wall

A full-screen TV mode for a venue's screen: the venue's name, then one machine from its floor at a time with that
game's all-time top 10 (rank, initials, nickname, score, date), rotating every 15 seconds with a progress bar and
"Machine 3 of 19".

Owner `lena` (Lena Gruber, community and tournaments, Graz). Goat Arcade data, all synthetic. Template `static`:
everything is in `public/` (`index.html`, `wall.css`, `wall.js`); `server.py` is the template's and unchanged.

- **URL:** `/?venue=LJ1` for a venue; without it, a chooser. `&seconds=30` slows the rotation (5 to 300).
  On the TV: `F` toggles full screen, `←` `→` step through the machines; the pointer hides after 3 s.
- **One slide per model.** The `leaderboard` view is per model (members' best games at every venue), so two
  cabinets of one title share a slide ("2 on the floor"). Models without leaderboard entries (air hockey, claw,
  roll-a-ball) are skipped. Scores set at this venue are marked "set here". Scores are never compared across models.
- **Out of order** machines keep their slide with a red badge ("1 of 2 out of order" when only one cabinet is down).
- **Data:** `venues` (name, chooser), `machine_floor` (what stands here now, `out_of_order`), `leaderboard` (filtered
  to the models on this floor). Fetched on load and every 5 minutes, never on a rotation; the machine on screen
  stays put across a refresh. A failed refresh keeps the old data and says so in the footer; a failed first load
  retries every 30 s. The footer shows the newest score in the data ("up to 18 Oct 2026") and the last update.
- **Look:** dark, high contrast, sized in viewport units for a TV or projector; system fonts only, so the screen
  needs no internet beyond Annex. `prefers-reduced-motion` turns off the fade and makes the progress bar step once
  a second.

## How it was built
Lena's request, as typed into Claude Code:

> I want a high-score wall for the TVs in our venues, owner lena. Full screen, dark, arcade style, big enough to read
> from the bar. Show the venue name and then one machine at a time from that venue's floor with its top 10 of all
> time: place, initials, nickname, score and date. Switch to the next machine every 15 seconds, with a little
> progress bar and "machine 3 of 19". The venue comes from the link, like ?venue=GZ, and give me a simple picker
> without one; ?seconds=30 for a slower one. Skip machines nobody has a score on, put an "out of order" badge on
> broken ones but still show their scores. Reload the data every five minutes, not every time it switches.

**Review pass** (after the first deploy):
- The leaderboard is per model, not per cabinet, so the rotation is one slide per model; otherwise a venue with two
  cabinets of a title showed the same table twice. The badge counts cabinets when only some are down.
- One timer for the fade and the switch: pressing an arrow key during the fade no longer starts a second rotation,
  and the next switch counts from when the machine appears, so the bar and the switch end together.
- A failed first load retries once per 30 s instead of starting a new retry chain on every 5-minute refresh.
- The header said "Goat Arcade Goat Arcade Metelkova" (venue names already carry the brand); now just the name.
- An unknown `?venue=` shows the chooser once; before, the 5-minute refresh appended the venue list again.
- The data's end is the newest score's time (compared as dates, not strings, since offsets differ in summer and
  winter).
