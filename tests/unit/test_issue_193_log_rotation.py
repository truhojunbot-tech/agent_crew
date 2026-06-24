"""Regression tests for issue #193 — dispatch log rotation.

`dispatch_*.log` and `attribution.jsonl` were append-only and grew
unboundedly (server.log 317MB, dispatch_tester.log 102MB after a week
of production). `_rotate_log_if_oversized` slides files
path → path.1 → path.2 → drop when the live file crosses the cap.
"""
import os

from agent_crew.server import _rotate_log_if_oversized


def test_b193_under_cap_is_noop(tmp_path):
    p = tmp_path / "dispatch.log"
    p.write_bytes(b"x" * (1024 * 100))   # 100KB
    _rotate_log_if_oversized(str(p), max_mb=1)
    assert p.exists()
    assert not (tmp_path / "dispatch.log.1").exists()


def test_b193_oversize_rotates_to_dot1(tmp_path):
    p = tmp_path / "dispatch.log"
    p.write_bytes(b"y" * (2 * 1024 * 1024))   # 2 MB
    _rotate_log_if_oversized(str(p), max_mb=1)
    assert not p.exists() or p.stat().st_size == 0, "live file should be gone or empty after rotation"
    rotated = tmp_path / "dispatch.log.1"
    assert rotated.exists()
    assert rotated.stat().st_size == 2 * 1024 * 1024


def test_b193_existing_dot1_shifts_to_dot2(tmp_path):
    p = tmp_path / "dispatch.log"
    p.write_bytes(b"new" * (700_000))     # ~2 MB
    (tmp_path / "dispatch.log.1").write_text("first-rotation")
    _rotate_log_if_oversized(str(p), max_mb=1, keep=3)
    assert (tmp_path / "dispatch.log.1").exists()
    assert (tmp_path / "dispatch.log.2").read_text() == "first-rotation"


def test_b193_oldest_is_dropped_at_keep_limit(tmp_path):
    p = tmp_path / "dispatch.log"
    p.write_bytes(b"q" * (2 * 1024 * 1024))
    (tmp_path / "dispatch.log.1").write_text("one")
    (tmp_path / "dispatch.log.2").write_text("two")
    (tmp_path / "dispatch.log.3").write_text("three")   # this should be dropped
    _rotate_log_if_oversized(str(p), max_mb=1, keep=3)
    assert (tmp_path / "dispatch.log.1").exists()
    assert (tmp_path / "dispatch.log.2").read_text() == "one"
    assert (tmp_path / "dispatch.log.3").read_text() == "two"
    # "three" must be gone — the cap is keep=3 numbered files.
    files = sorted(os.listdir(tmp_path))
    assert "dispatch.log.4" not in files


def test_b193_missing_file_is_noop(tmp_path):
    # Doesn't raise even if path doesn't exist.
    _rotate_log_if_oversized(str(tmp_path / "nope.log"), max_mb=1)
