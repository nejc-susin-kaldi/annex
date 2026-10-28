import json

from annex_ops import config


def test_defaults_then_env_then_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TUNING_FILE", str(tmp_path / "tuning.json"))
    config._tuning_cache.update(mtime=None, values={})
    assert config.tuning().stopped_grace_s == 60 and config.tuning().crash_count == 3
    monkeypatch.setenv("OPS_CRASH_COUNT", "5")
    assert config.tuning().crash_count == 5
    (tmp_path / "tuning.json").write_text(json.dumps({"stopped_grace_s": 10, "error_ratio": 0.5, "crash_count": "7"}))
    t = config.tuning()
    assert (t.stopped_grace_s, t.error_ratio, t.crash_count) == (10, 0.5, 7)  # the file wins, values keep their type
    (tmp_path / "tuning.json").unlink()
    assert config.tuning().stopped_grace_s == 60  # no file: back to the defaults


def test_a_broken_file_or_value_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TUNING_FILE", str(tmp_path / "tuning.json"))
    config._tuning_cache.update(mtime=None, values={})
    (tmp_path / "tuning.json").write_text("{not json")
    assert config.tuning().tick_s == 15
    (tmp_path / "tuning.json").write_text(json.dumps({"tick_s": "soon"}))
    assert config.tuning().tick_s == 15
