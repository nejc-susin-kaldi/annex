"""Unit tests for tilts.py: uv run --python 3.13 --with pytest pytest tests -q (from the app folder)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tilts  # noqa: E402

MODELS = [{"model_id": 12, "title": "Cyber Samurai"}, {"model_id": 7, "title": "Jet Set"},
          {"model_id": 9, "title": "Lonely Lion"}]
FLOOR = [
    {"machine_id": 1, "model_id": 12, "venue_code": "GZ", "out_of_order": False, "open_ticket": True},
    {"machine_id": 2, "model_id": 12, "venue_code": "LJ1", "out_of_order": False, "open_ticket": False},
    {"machine_id": 3, "model_id": 12, "venue_code": "ZG", "out_of_order": True, "open_ticket": True},
    {"machine_id": 5, "model_id": 9, "venue_code": "MB", "out_of_order": False, "open_ticket": False},
]


def play(mid, title, games, tilts_, day="2026-10-20", venue="GZ", machine_type="pinball"):
    return {"day": day, "venue_code": venue, "machine_id": mid, "title": title, "era": "LCD",
            "machine_type": machine_type, "games": games, "tilts": tilts_}


def test_rates_against_the_other_cabinets():
    plays = [play(1, "Cyber Samurai", 100, 7), play(1, "Cyber Samurai", 100, 7, day="2026-10-19"),
             play(2, "Cyber Samurai", 300, 6, venue="LJ1"), play(3, "Cyber Samurai", 100, 4, venue="ZG")]
    cabs = {c.machine_id: c for c in tilts.cabinets(plays, FLOOR, MODELS)}
    gz = cabs[1]
    assert (gz.games, gz.tilts, gz.days, gz.peers) == (200, 14, 2, 2)
    assert gz.rate == 0.07
    assert gz.model_rate == 10 / 400          # the other two, games-weighted; not including itself
    assert round(gz.ratio, 2) == 2.8
    assert gz.open_ticket and not gz.out_of_order
    assert cabs[3].out_of_order


def test_flagged_worst_first_and_noise_ignored():
    plays = [play(1, "Cyber Samurai", 200, 28), play(2, "Cyber Samurai", 400, 8, venue="LJ1"),
             play(3, "Cyber Samurai", 100, 2, venue="ZG"),
             # a lone cabinet: nothing to compare with, never flagged
             play(5, "Lonely Lion", 100, 40, venue="MB")]
    cabs = tilts.cabinets(plays, FLOOR, MODELS)
    hits = tilts.flagged(cabs, 3.0, 5)
    assert [c.machine_id for c in hits] == [1]
    assert round(hits[0].ratio, 1) == 7.0     # 14% against 10/500 = 2%
    lone = next(c for c in cabs if c.machine_id == 5)
    assert lone.ratio is None and lone.peers == 0
    assert [c.machine_id for c in tilts.ranked(cabs)][-1] == 5


def test_too_few_tilts_is_not_a_finding():
    plays = [play(1, "Cyber Samurai", 30, 3), play(2, "Cyber Samurai", 400, 2, venue="LJ1")]
    assert tilts.flagged(tilts.cabinets(plays, FLOOR, MODELS), 3.0, 5) == []


def test_others_that_never_tilt_still_flag_a_tilting_cabinet():
    plays = [play(1, "Cyber Samurai", 200, 9), play(2, "Cyber Samurai", 400, 0, venue="LJ1")]
    hits = tilts.flagged(tilts.cabinets(plays, FLOOR, MODELS), 3.0, 5)
    assert [c.machine_id for c in hits] == [1] and hits[0].ratio is None


def test_machine_in_storage_and_moves():
    # machine 4 isn't on a floor: its model comes from machine_models by title; its last venue is its latest day
    plays = [play(4, "Jet Set", 50, 1, day="2026-10-01", venue="KP"), play(4, "Jet Set", 50, 1, day="2026-10-03", venue="CE"),
             play(9, "Video Thing", 99, 0, machine_type="video")]
    (c,) = tilts.cabinets(plays, FLOOR, MODELS)
    assert (c.model_id, c.venue_code, c.last_venue, c.games) == (7, None, "CE", 100)


def test_format():
    assert tilts.pct(0.1324) == "13.2%" and tilts.pct(None) == "–"
    assert tilts.times(5.24) == "5.2×" and tilts.times(None) == "–"
