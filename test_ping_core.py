"""Tests du cœur : python test_ping_core.py (ou pytest)."""
import os
import statistics
import subprocess
import tempfile
from types import SimpleNamespace

import ping_core as pc


def test_log_round_trip_skips_errors():
    lines = [pc.format_log_line("2026-01-01 00:00:00", "h", "OK", 5),
             pc.format_log_line("2026-01-01 00:00:01", "h", "TIMEOUT"),
             pc.format_log_line("2026-01-01 00:00:02", "h", "ERROR", extra="boom\tmulti\nline")]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "log.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        assert pc.parse_log_file(path) == [["2026-01-01 00:00:00", "h", 5, "OK"],
                                           ["2026-01-01 00:00:01", "h", None, "TIMEOUT"]]


def test_summary_matches_statistics():
    lats = [12, 15, None, 11, 40, None, 13]
    got = [v for v in lats if v is not None]
    s = pc.summarize(lats)
    assert (s["total"], s["lost"], s["min"], s["max"]) == (7, 2, 11, 40)
    assert abs(s["stdev"] - statistics.stdev(got)) < 1e-9
    assert s["median"] == statistics.median(got)
    assert s["jitter"] == (3 + 4 + 29 + 27) / 4      # écarts entre réponses successives
    assert pc.compute_quality(pc.summarize([10] * 10)) == (100, "A")
    assert pc.compute_quality(pc.summarize([None] * 10))[1] == "F"


def _fake_run(stdout=None, raises=None):
    def run(*_a, **_kw):
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout)
    return run


def test_ping_once_any_language_and_timeout():
    real = pc.subprocess.run
    try:
        for out, want in [("Reply from 8.8.8.8: bytes=32 time<1ms TTL=117", 1),
                          ("Réponse de 8.8.8.8 : octets=32 temps=12 ms TTL=117", 12),
                          ("Antwort von 8.8.8.8: Bytes=32 Zeit=12ms TTL=117\n"
                           "    Minimum = 99ms, Maximum = 99ms, Mittelwert = 99ms", 12),
                          ("Ответ от 8.8.8.8: число байт=32 время=12мс TTL=117", 12),
                          ("Reply from 192.168.1.10: Destination host unreachable.", None),
                          ("Request timed out.", None)]:
            pc.subprocess.run = _fake_run(stdout=out)
            assert pc.ping_once("x")[0] == want, out
        pc.subprocess.run = _fake_run(raises=subprocess.TimeoutExpired("ping", 5))
        assert pc.ping_once("x") == (None, "")
    finally:
        pc.subprocess.run = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
