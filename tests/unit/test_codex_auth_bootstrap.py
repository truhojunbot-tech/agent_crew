"""Codex auth bootstrap into worktree-local CODEX_HOME (Issue #110 phase 5b).

`_get_agent_cmd("codex", worktree)` points codex at `<wt>/.codex_local`,
which means codex no longer falls back to `~/.codex` and would prompt
for a fresh OAuth flow on every worktree. To preserve the operator's
existing login, `setup._write_mcp_config_codex` mirrors
`~/.codex/auth.json` into the worktree's CODEX_HOME at config-write
time. These tests pin that contract.
"""
import os
import stat
from unittest.mock import patch

from agent_crew import setup as crew_setup


class TestBootstrapCodexAuth:
    def test_copies_global_auth_into_codex_home(self, tmp_path):
        # Synthetic ~/.codex/auth.json at a fake HOME so the test never
        # touches the real one.
        fake_home = tmp_path / "fake_home"
        global_dir = fake_home / ".codex"
        global_dir.mkdir(parents=True)
        global_auth = global_dir / "auth.json"
        global_auth.write_text("{\"token\":\"abc123\"}")
        os.chmod(global_auth, 0o600)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        codex_home = worktree / ".codex_local"
        codex_home.mkdir()

        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            crew_setup._bootstrap_codex_auth(str(codex_home))

        copied = codex_home / "auth.json"
        assert copied.exists()
        assert copied.read_text() == "{\"token\":\"abc123\"}"
        # Mode must be 0600 — credentials file.
        mode = stat.S_IMODE(copied.stat().st_mode)
        assert mode == 0o600

    def test_silent_skip_when_global_missing(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()  # no .codex/ dir

        worktree = tmp_path / "wt"
        worktree.mkdir()
        codex_home = worktree / ".codex_local"
        codex_home.mkdir()

        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            # Must NOT raise.
            crew_setup._bootstrap_codex_auth(str(codex_home))

        assert not (codex_home / "auth.json").exists()

    def test_idempotent_on_existing_local_auth(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        global_dir = fake_home / ".codex"
        global_dir.mkdir(parents=True)
        global_auth = global_dir / "auth.json"
        global_auth.write_text("{\"token\":\"new\"}")

        worktree = tmp_path / "wt"
        worktree.mkdir()
        codex_home = worktree / ".codex_local"
        codex_home.mkdir()
        # Pre-existing local auth from a stale setup run.
        local_auth = codex_home / "auth.json"
        local_auth.write_text("{\"token\":\"stale\"}")

        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            crew_setup._bootstrap_codex_auth(str(codex_home))

        # Bootstrap should refresh the local copy with the global one
        # (codex rewrites this file on token refresh, so always-up-to-date
        # is the right contract).
        assert local_auth.read_text() == "{\"token\":\"new\"}"


class TestWriteMcpConfigCodexCallsBootstrap:
    """`write_mcp_config(agent="codex", ...)` must run the auth
    bootstrap as part of its work, so a single `crew setup` /
    `crew recover` call leaves codex fully ready."""

    def test_write_mcp_config_codex_writes_auth_when_global_exists(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        global_dir = fake_home / ".codex"
        global_dir.mkdir(parents=True)
        (global_dir / "auth.json").write_text("{\"x\":1}")

        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            crew_setup.write_mcp_config(str(worktree), "/db.db", agent="codex")

        # config.toml + auth.json both materialise.
        assert (worktree / ".codex_local" / "config.toml").exists()
        assert (worktree / ".codex_local" / "auth.json").exists()

    def test_write_mcp_config_codex_no_auth_when_global_missing(self, tmp_path):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            crew_setup.write_mcp_config(str(worktree), "/db.db", agent="codex")

        # config.toml still written; auth.json silently skipped.
        assert (worktree / ".codex_local" / "config.toml").exists()
        assert not (worktree / ".codex_local" / "auth.json").exists()
