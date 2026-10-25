"""#831: the premerge cron prepares its scratch dir and tolerates GitHub outages."""

from datetime import datetime, timezone

import pytest

from scripts import premerge_gate as gate

HEAD = "a" * 40
MAIN = "b" * 40


def _pr():
    return {"number": 831, "head": {"sha": HEAD,
                                     "repo": {"full_name": gate.REPO}}}


def _approval():
    return [{"body": f"{gate.REVIEW} ✅ approve\nReviewed PR at {HEAD}",
             "author_association": "OWNER", "created_at": "2026-10-25T00:00:00Z",
             "id": 1}]


def test_main_creates_missing_work_dir_before_testing(tmp_path, monkeypatch):
    work_dir = tmp_path / "not-created" / "scratch"
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr()] if "/pulls?" in endpoint else _approval())
    monkeypatch.setattr(gate, "checked", lambda args, **_kwargs:
                        MAIN if args == ["git", "rev-parse", "origin/main"] else
                        HEAD if args == ["git", "rev-parse", "FETCH_HEAD"] else "")
    tested = []

    def fake_test_pr(number, head, main, passed_work_dir):
        assert work_dir.is_dir()
        tested.append((number, head, main, passed_work_dir))
        return "1 passed", 0

    monkeypatch.setattr(gate, "test_pr", fake_test_pr)
    assert gate.main(["--lock-file", str(tmp_path / "gate.lock"),
                      "--work-dir", str(work_dir)]) == 0
    assert tested == [(831, HEAD, MAIN, str(work_dir))]


@pytest.mark.parametrize("message", [
    "TLS handshake timeout", "i/o timeout", "connection reset by peer",
    "connection refused", "could not resolve host", "HTTP 503 Service Unavailable",
])
def test_open_pr_list_network_error_skips_run(tmp_path, monkeypatch, capsys, message):
    def unavailable(_endpoint):
        raise RuntimeError(message)

    monkeypatch.setattr(gate, "_api_list", unavailable)
    monkeypatch.setattr(gate, "test_pr", lambda *_args:
                        pytest.fail("a PR was tested after list failure"))
    assert gate.main(["--lock-file", str(tmp_path / "gate.lock")]) == 0
    captured = capsys.readouterr()
    assert captured.err.strip() == f"premerge gate: skipped run, GitHub unreachable: {message}"
    assert "Traceback" not in captured.err


def test_open_pr_list_other_error_still_propagates(tmp_path, monkeypatch):
    def bad_response(_endpoint):
        raise RuntimeError("malformed PR list")

    monkeypatch.setattr(gate, "_api_list", bad_response)
    with pytest.raises(RuntimeError, match="malformed PR list"):
        gate.main(["--lock-file", str(tmp_path / "gate.lock")])


def test_network_skip_message_is_one_line(tmp_path, monkeypatch, capsys):
    def unavailable(_endpoint):
        raise RuntimeError("TLS handshake timeout\nretry later")

    monkeypatch.setattr(gate, "_api_list", unavailable)
    assert gate.main(["--lock-file", str(tmp_path / "gate.lock")]) == 0
    assert capsys.readouterr().err.splitlines() == [
        "premerge gate: skipped run, GitHub unreachable: TLS handshake timeout retry later",
    ]


def test_run_start_is_printed_only_after_lock_acquisition(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "gate.lock"
    monkeypatch.setattr(gate, "_api_list", lambda _endpoint: [])

    assert gate.main(["--lock-file", str(lock)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    prefix = "premerge gate: run start "
    assert output.out.startswith(prefix)
    assert datetime.fromisoformat(output.out[len(prefix):].strip()).tzinfo == timezone.utc

    with gate.single_run_lock(lock):
        assert gate.main(["--lock-file", str(lock)]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "premerge gate already running" in output.err


def test_per_pr_result_print_is_flushed(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr()] if "/pulls?" in endpoint else _approval())
    monkeypatch.setattr(gate, "checked", lambda args, **_kwargs:
                        MAIN if args == ["git", "rev-parse", "origin/main"] else
                        HEAD if args == ["git", "rev-parse", "FETCH_HEAD"] else "")
    monkeypatch.setattr(gate, "test_pr", lambda *_args: ("1 passed", 0))
    printed = []
    monkeypatch.setattr(gate, "print", lambda message, **kwargs:
                        printed.append((message, kwargs)), raising=False)

    assert gate.main(["--lock-file", str(tmp_path / "gate.lock")]) == 0
    assert any(message.startswith("PR #831: 1 passed") and kwargs.get("flush") is True
               for message, kwargs in printed)
