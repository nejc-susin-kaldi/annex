import json

from annex_ops import hooks_config as hc


def test_load_reads_and_rereads_when_the_file_changes(tmp_path):
    f = tmp_path / "probe.json"
    f.write_text(json.dumps({"token": "t1", "port": 8080, "hooks": {"trello": {"verify": "trello", "secret": "s", "path": "/hooks/trello"}}}))
    assert hc.hook("probe", "trello", str(tmp_path))["path"] == "/hooks/trello"
    assert hc.load("probe", str(tmp_path))["token"] == "t1"
    f.write_text(json.dumps({"token": "t2", "port": 8080, "hooks": {}}))
    import os
    os.utime(f, (os.stat(f).st_atime + 5, os.stat(f).st_mtime + 5))
    assert hc.load("probe", str(tmp_path))["token"] == "t2"
    assert hc.hook("probe", "trello", str(tmp_path)) is None


def test_missing_app_or_hook_is_none_and_names_cannot_escape_the_directory(tmp_path):
    (tmp_path / "x.json").write_text("{}")
    assert hc.load("nope", str(tmp_path)) is None
    for bad in ("../x", "..", "a/b", "A", "", "x.json"):
        assert hc.load(bad, str(tmp_path)) is None


def test_a_broken_file_is_none(tmp_path):
    (tmp_path / "probe.json").write_text("{not json")
    assert hc.load("probe", str(tmp_path)) is None
