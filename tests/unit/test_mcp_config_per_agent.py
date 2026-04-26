"""Per-agent MCP config writing (Issue #110 phase 5).

`write_mcp_config(worktree_path, db_path, agent=...)` now writes to the
file the named agent's CLI actually reads at session start:

  claude → `<wt>/.mcp.json`                       (Claude Code default)
  codex  → `<wt>/.codex_local/config.toml`        (CODEX_HOME=...
                                                   override on launch)
  gemini → `<wt>/.gemini/settings.json`           (project-scope auto-
                                                   discovered when run
                                                   from the worktree)

The earlier all-`.mcp.json` setup was invisible to codex/gemini, which
is why those agents fell back to HTTP polling and never picked up MCP
tools — see issue #110 comments.
"""
import json
import os

from agent_crew import setup as crew_setup


# ---------------------------------------------------------------------------
# claude (.mcp.json) — preserved from phase 2
# ---------------------------------------------------------------------------


class TestClaudeMcpConfig:
    def test_writes_dot_mcp_json_at_worktree_root(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/db.db", agent="claude")
        assert path == os.path.abspath(str(wt / ".mcp.json"))
        config = json.loads((wt / ".mcp.json").read_text())
        assert "agent_crew" in config["mcpServers"]
        assert config["mcpServers"]["agent_crew"]["env"]["AGENT_CREW_DB"] == "/db.db"

    def test_default_agent_is_claude(self, tmp_path):
        # write_mcp_config without `agent=` falls back to claude path,
        # mirroring the legacy phase-2 contract.
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/db.db")
        assert path.endswith(".mcp.json")


# ---------------------------------------------------------------------------
# codex (.codex_local/config.toml + CODEX_HOME)
# ---------------------------------------------------------------------------


class TestCodexMcpConfig:
    def test_writes_codex_local_config_toml(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/db.db", agent="codex")
        assert path == os.path.abspath(str(wt / ".codex_local" / "config.toml"))
        body = (wt / ".codex_local" / "config.toml").read_text()
        # TOML format: section header + key=value lines.
        assert "[mcp_servers.agent_crew]" in body
        assert "agent_crew.mcp_server" in body
        assert 'AGENT_CREW_DB = "/db.db"' in body
        assert "PYTHONPATH = " in body

    def test_get_agent_cmd_prefixes_codex_home(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        cmd = crew_setup._get_agent_cmd("codex", worktree_path=str(wt))
        assert cmd.startswith("CODEX_HOME=")
        assert ".codex_local" in cmd
        assert "codex --dangerously-bypass-approvals-and-sandbox" in cmd

    def test_get_agent_cmd_no_prefix_when_no_worktree(self):
        # Operator-invoked codex without a worktree context → no prefix.
        cmd = crew_setup._get_agent_cmd("codex", worktree_path=None)
        assert "CODEX_HOME=" not in cmd

    def test_overwrites_existing_config(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/v1.db", agent="codex")
        crew_setup.write_mcp_config(str(wt), "/v2.db", agent="codex")
        body = (wt / ".codex_local" / "config.toml").read_text()
        assert '"/v2.db"' in body
        assert '"/v1.db"' not in body


# ---------------------------------------------------------------------------
# gemini (.gemini/settings.json) — JSON, project-scope auto-discovery
# ---------------------------------------------------------------------------


class TestGeminiMcpConfig:
    def test_writes_dot_gemini_settings_json(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/db.db", agent="gemini")
        assert path == os.path.abspath(str(wt / ".gemini" / "settings.json"))
        data = json.loads((wt / ".gemini" / "settings.json").read_text())
        assert "agent_crew" in data["mcpServers"]
        assert data["mcpServers"]["agent_crew"]["env"]["AGENT_CREW_DB"] == "/db.db"

    def test_preserves_existing_settings_keys(self, tmp_path):
        wt = tmp_path / "wt"
        gemini_dir = wt / ".gemini"
        gemini_dir.mkdir(parents=True)
        # Project may already have other gemini config (theme, security, etc.)
        existing = {
            "security": {"auth": {"selectedType": "oauth-personal"}},
            "ui": {"theme": "dark"},
        }
        (gemini_dir / "settings.json").write_text(json.dumps(existing))

        crew_setup.write_mcp_config(str(wt), "/db.db", agent="gemini")
        data = json.loads((gemini_dir / "settings.json").read_text())
        assert data["security"]["auth"]["selectedType"] == "oauth-personal"
        assert data["ui"]["theme"] == "dark"
        assert "agent_crew" in data["mcpServers"]

    def test_merges_into_existing_mcp_servers_block(self, tmp_path):
        wt = tmp_path / "wt"
        gemini_dir = wt / ".gemini"
        gemini_dir.mkdir(parents=True)
        existing = {
            "mcpServers": {
                "other_tool": {"command": "tool", "args": []},
            }
        }
        (gemini_dir / "settings.json").write_text(json.dumps(existing))

        crew_setup.write_mcp_config(str(wt), "/db.db", agent="gemini")
        data = json.loads((gemini_dir / "settings.json").read_text())
        # Both entries present.
        assert "other_tool" in data["mcpServers"]
        assert "agent_crew" in data["mcpServers"]

    def test_overwrites_only_agent_crew_entry(self, tmp_path):
        wt = tmp_path / "wt"
        gemini_dir = wt / ".gemini"
        gemini_dir.mkdir(parents=True)
        existing = {
            "mcpServers": {
                "other_tool": {"command": "tool", "args": []},
                "agent_crew": {"command": "stale", "args": []},
            }
        }
        (gemini_dir / "settings.json").write_text(json.dumps(existing))

        crew_setup.write_mcp_config(str(wt), "/v2.db", agent="gemini")
        data = json.loads((gemini_dir / "settings.json").read_text())
        assert data["mcpServers"]["other_tool"]["command"] == "tool"
        assert data["mcpServers"]["agent_crew"]["command"] != "stale"
        assert data["mcpServers"]["agent_crew"]["env"]["AGENT_CREW_DB"] == "/v2.db"

    def test_handles_invalid_json_gracefully(self, tmp_path):
        """A corrupt settings.json shouldn't crash; the agent_crew block
        gets written and any non-recoverable fields are dropped."""
        wt = tmp_path / "wt"
        gemini_dir = wt / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "settings.json").write_text("{ this is not json")

        crew_setup.write_mcp_config(str(wt), "/db.db", agent="gemini")
        data = json.loads((gemini_dir / "settings.json").read_text())
        assert "agent_crew" in data["mcpServers"]


# ---------------------------------------------------------------------------
# write_mcp_configs(worktrees, db) dispatches by agent name
# ---------------------------------------------------------------------------


class TestWriteMcpConfigsDispatch:
    def test_each_agent_gets_its_own_layout(self, tmp_path):
        wt_c = tmp_path / "claude"
        wt_x = tmp_path / "codex"
        wt_g = tmp_path / "gemini"
        for p in (wt_c, wt_x, wt_g):
            p.mkdir()
        worktrees = {
            "claude": str(wt_c),
            "codex": str(wt_x),
            "gemini": str(wt_g),
        }
        crew_setup.write_mcp_configs(worktrees, "/db.db")
        assert (wt_c / ".mcp.json").exists()
        assert (wt_x / ".codex_local" / "config.toml").exists()
        assert (wt_g / ".gemini" / "settings.json").exists()
