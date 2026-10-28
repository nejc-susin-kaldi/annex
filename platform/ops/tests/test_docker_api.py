import io

from annex_ops import docker_api


def frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def test_frames_demultiplex_stdout_and_stderr():
    raw = frame(1, b"out one\n") + frame(2, b"err one\n") + frame(1, b"out two\n")
    assert list(docker_api.frames(io.BytesIO(raw).read)) == [("stdout", b"out one\n"), ("stderr", b"err one\n"), ("stdout", b"out two\n")]


def test_a_truncated_stream_stops_cleanly():
    raw = frame(1, b"complete\n") + frame(1, b"cut off\n")[:-3]
    assert list(docker_api.frames(io.BytesIO(raw).read)) == [("stdout", b"complete\n")]
    assert list(docker_api.frames(io.BytesIO(b"\x01\x00").read)) == []


def test_lines_join_text_split_across_frames_and_keep_streams_apart():
    raw = frame(1, b"first li") + frame(2, b"E1\n") + frame(1, b"ne\nsecond\n") + frame(1, b"tail no newline")
    got = list(docker_api.lines(docker_api.frames(io.BytesIO(raw).read)))
    assert got == [("stderr", "E1"), ("stdout", "first line"), ("stdout", "second"), ("stdout", "tail no newline")]


def test_several_lines_in_one_frame_and_crlf():
    raw = frame(1, b"a\r\nb\nc\n")
    assert [ln for _, ln in docker_api.lines(docker_api.frames(io.BytesIO(raw).read))] == ["a", "b", "c"]


def test_parse_ts_with_nanoseconds():
    ts = docker_api.parse_ts("2026-10-15T13:53:31.981769651Z")
    assert abs(ts - 1792072411.981769651) < 1e-5
    assert docker_api.parse_ts("2026-10-15T13:53:31Z") == 1792072411.0
    assert docker_api.parse_ts("not a time") is None


def test_split_timestamp_prefix():
    ts, rest = docker_api.split_timestamp('2026-10-15T13:53:31.5Z {"a": 1} with spaces')
    assert ts == 1792072411.5 and rest == '{"a": 1} with spaces'
    assert docker_api.split_timestamp("no timestamp here") == (None, "no timestamp here")
