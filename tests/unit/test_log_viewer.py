"""Regression tests for log_viewer.py.

Bug: _process_line assumed json.loads returned a dict and called .get on it.
When codex emitted a line containing a bare JSON literal like `1` or
`4598510`, the parser returned an int and the viewer crashed with
`AttributeError: 'int' object has no attribute 'get'`. The reviewer pane
(running the log viewer for codex output) died and dropped to bash.
"""
from unittest.mock import patch

import pytest

from agent_crew.log_viewer import _process_line, tail_and_format


def test_bare_int_line_does_not_crash():
    out = _process_line("4598510")
    assert out is not None
    assert "4598510" in out


def test_bare_list_line_does_not_crash():
    out = _process_line("[1, 2, 3]")
    assert out is not None


def test_bare_string_line_does_not_crash():
    out = _process_line('"just a string"')
    assert out is not None


def test_non_json_line_still_falls_back_to_plain_text():
    out = _process_line("plain non-json text")
    assert out is not None
    assert "plain non-json text" in out


def test_assistant_dict_still_handled_normally():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
    out = _process_line(line)
    assert out is not None
    assert "hi" in out


def test_empty_line_returns_none():
    assert _process_line("") is None
    assert _process_line("   \n") is None


class _StopTest(Exception):
    """Sentinel to break out of tail_and_format's infinite loop in tests."""


class _FakeFile:
    def __init__(self, calls):
        self._calls = calls
        self._i = 0

    def readline(self):
        action = self._calls[self._i]
        self._i += 1
        if action is KeyboardInterrupt:
            raise KeyboardInterrupt
        if action is _StopTest:
            raise _StopTest
        return action

    def close(self):
        pass


def test_ctrl_c_does_not_kill_the_viewer(capsys):
    """Regression: a pane operator hitting Ctrl+C used to kill the passive
    log-viewer process and drop the pane to a bare shell, which then looked
    like a crashed agent. The viewer only tails and prints — there's nothing
    for Ctrl+C to usefully interrupt, so it must survive SIGINT.
    """
    fake = _FakeFile([KeyboardInterrupt, "hello\n", _StopTest])
    with patch("agent_crew.log_viewer.open", return_value=fake, create=True), \
         patch("agent_crew.log_viewer.time.sleep"):
        with pytest.raises(_StopTest):
            tail_and_format("/fake/path")
    out = capsys.readouterr().out
    assert "hello" in out
