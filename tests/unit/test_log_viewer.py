"""Regression tests for log_viewer.py.

Bug: _process_line assumed json.loads returned a dict and called .get on it.
When codex emitted a line containing a bare JSON literal like `1` or
`4598510`, the parser returned an int and the viewer crashed with
`AttributeError: 'int' object has no attribute 'get'`. The reviewer pane
(running the log viewer for codex output) died and dropped to bash.
"""
from agent_crew.log_viewer import _process_line


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
