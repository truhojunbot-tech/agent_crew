"""Regression test: gemini session files larger than the cap are archived.

Bug: ``gemini -p --resume latest`` re-loads the most recent jsonl on every
dispatch. With no size bound the session can grow to hundreds of MB and
every subsequent kickoff fails immediately with
`The input token count exceeds the maximum number of tokens allowed 1048576`,
silently bricking the entire tester role.
"""
import json
import os
from unittest.mock import patch

from agent_crew.server import _cap_gemini_session_size


def test_oversized_session_is_archived(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    project_dir = "gemini-99"
    chats = home / ".gemini" / "tmp" / project_dir / "chats"
    chats.mkdir(parents=True)

    big = chats / "session-big.jsonl"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    small = chats / "session-small.jsonl"
    small.write_bytes(b"y" * 1024)

    projects_path = home / ".gemini" / "projects.json"
    projects_path.write_text(json.dumps({"projects": {str(cwd): project_dir}}))

    with patch.dict(os.environ, {"HOME": str(home)}):
        with patch("pathlib.Path.home", return_value=home):
            _cap_gemini_session_size(str(cwd), max_mb=1)

    assert not big.exists(), "oversized file should have been moved"
    assert (chats / "_archive" / "session-big.jsonl").exists(), \
        "archived copy should live under chats/_archive/"
    assert small.exists(), "small files should remain in place"


def test_missing_projects_json_is_noop(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    with patch("pathlib.Path.home", return_value=home):
        _cap_gemini_session_size(str(cwd), max_mb=1)


def test_unknown_cwd_is_noop(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "projects.json").write_text(
        json.dumps({"projects": {"/some/other/path": "gemini-99"}})
    )
    with patch("pathlib.Path.home", return_value=home):
        _cap_gemini_session_size(str(cwd), max_mb=1)


def test_under_cap_files_are_kept(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    project_dir = "gemini-99"
    chats = home / ".gemini" / "tmp" / project_dir / "chats"
    chats.mkdir(parents=True)

    sess = chats / "session-keep.jsonl"
    sess.write_bytes(b"z" * (512 * 1024))

    (home / ".gemini" / "projects.json").write_text(
        json.dumps({"projects": {str(cwd): project_dir}})
    )

    with patch("pathlib.Path.home", return_value=home):
        _cap_gemini_session_size(str(cwd), max_mb=1)

    assert sess.exists()
    assert not (chats / "_archive").exists()
