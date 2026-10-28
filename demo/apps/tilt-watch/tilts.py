"""tilt-watch's pure part: tilt rates per pinball cabinet against its model's average. No network, no clock."""
from dataclasses import dataclass
from datetime import date


@dataclass
class Cabinet:
    machine_id: int
    title: str
    model_id: int | None
    era: str | None
    venue_code: str | None       # where it stands now (machine_floor); None: not on a floor
    last_venue: str | None       # where it stood on its last day with play in the window
    out_of_order: bool
    open_ticket: bool
    games: int
    tilts: int
    days: int                    # days with play in the window
    peers: int = 0               # other cabinets of the same model with play in the window
    peer_games: int = 0
    peer_tilts: int = 0

    @property
    def rate(self) -> float:
        return self.tilts / self.games if self.games else 0.0

    @property
    def model_rate(self) -> float | None:
        """The model's average tilt rate on its *other* cabinets (games-weighted); None if it's the only one."""
        return self.peer_tilts / self.peer_games if self.peer_games else None

    @property
    def ratio(self) -> float | None:
        """This cabinet's rate over its model's average. None: no other cabinet, or they never tilted."""
        m = self.model_rate
        return self.rate / m if m else None


def as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def cabinets(plays: list[dict], floor: list[dict], models: list[dict]) -> list[Cabinet]:
    """One Cabinet per pinball machine that was played in `plays` (daily_plays rows of the window), with its model's
    totals on the other cabinets. Machines move between venues; tilts count for the cabinet wherever it stood.
    The model comes from machine_floor (machines on a floor) or from machine_models by title (machines in storage)."""
    by_title = {m["title"]: m["model_id"] for m in models}
    on_floor = {f["machine_id"]: f for f in floor}
    acc: dict[int, dict] = {}
    for p in plays:
        if p.get("machine_type") not in (None, "pinball"):
            continue
        a = acc.setdefault(p["machine_id"], {"title": p["title"], "era": p.get("era"), "games": 0, "tilts": 0,
                                             "days": 0, "last_day": None, "last_venue": None})
        a["games"] += int(p["games"] or 0)
        a["tilts"] += int(p["tilts"] or 0)
        a["days"] += 1
        d = as_date(p["day"])
        if a["last_day"] is None or d >= a["last_day"]:
            a["last_day"], a["last_venue"] = d, p["venue_code"]
    out = []
    for mid, a in acc.items():
        if not a["games"]:
            continue
        f = on_floor.get(mid)
        out.append(Cabinet(
            machine_id=mid, title=a["title"], era=a["era"],
            model_id=f["model_id"] if f else by_title.get(a["title"]),
            venue_code=f["venue_code"] if f else None, last_venue=a["last_venue"],
            out_of_order=bool(f and f.get("out_of_order")), open_ticket=bool(f and f.get("open_ticket")),
            games=a["games"], tilts=a["tilts"], days=a["days"]))
    # the model's totals, then each cabinet's peers = the model minus itself
    totals: dict = {}
    for c in out:
        key = c.model_id if c.model_id is not None else ("title", c.title)
        t = totals.setdefault(key, [0, 0, 0])
        t[0] += 1
        t[1] += c.games
        t[2] += c.tilts
    for c in out:
        n, g, t = totals[c.model_id if c.model_id is not None else ("title", c.title)]
        c.peers, c.peer_games, c.peer_tilts = n - 1, g - c.games, t - c.tilts
    return out


def flagged(cabs: list[Cabinet], factor: float = 3.0, min_tilts: int = 5, min_peer_games: int = 50) -> list[Cabinet]:
    """Cabinets tilting more than `factor` times their model's average, worst first.
    A handful of tilts (fewer than `min_tilts`) or a model average from too few games (`min_peer_games`) is noise,
    not a finding. Other cabinets that never tilted give no ratio; such a cabinet is flagged once it has min_tilts."""
    hits = []
    for c in cabs:
        if c.tilts < min_tilts or c.peers == 0 or c.peer_games < min_peer_games:
            continue
        if c.ratio is None or c.ratio > factor:
            hits.append(c)
    return sorted(hits, key=lambda c: (c.ratio is not None, -(c.ratio or 0), -c.rate))


def ranked(cabs: list[Cabinet]) -> list[Cabinet]:
    """Every cabinet, highest ratio first; cabinets without a ratio (only one of their model) last, by rate."""
    return sorted(cabs, key=lambda c: (c.ratio is None, -(c.ratio or 0), -c.rate, c.title))


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def times(x: float | None) -> str:
    return "–" if x is None else f"{x:.1f}×"
