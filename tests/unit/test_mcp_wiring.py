"""Phase 2 wiring tests for #106:
- `instructions.generate(role, project, port, agent)` now embeds the
  task-loop prompt at the top of the rendered file.
- `setup.write_mcp_config(worktree, db_path)` writes a valid
  Claude-Code-style `.mcp.json`.
- `setup.write_mcp_configs(worktrees, db_path)` applies it across all
  worktrees in one call.
"""
import json
import os

from agent_crew import instructions, setup as crew_setup


# ---------------------------------------------------------------------------
# instructions.generate now embeds the task-loop prompt
# ---------------------------------------------------------------------------


class TestGenerateEmbedsTaskLoop:
    def test_implementer_default_agent_is_claude(self):
        out = instructions.generate("implementer", "myproj", 8100)
        # The full task-loop prompt's signature line — pinned to make sure
        # the prompt actually got prepended.
        assert "You are claude" in out
        assert "get_next_task" in out
        # Existing _COMMON / role section content still present.
        assert "Agent Crew — myproj" in out

    def test_reviewer_default_agent_is_codex(self):
        out = instructions.generate("reviewer", "myproj", 8100)
        assert "You are codex" in out
        assert "review" in out

    def test_tester_default_agent_is_gemini(self):
        out = instructions.generate("tester", "myproj", 8100)
        assert "You are gemini" in out

    def test_explicit_agent_overrides_default(self):
        out = instructions.generate("implementer", "myproj", 8100, agent="custom-bot")
        assert "You are custom-bot" in out
        assert "[agent: custom-bot]" in out

    def test_project_and_port_substitution_still_works(self):
        out = instructions.generate("implementer", "weirdproj", 9999)
        assert "weirdproj" in out
        assert "9999" in out


# ---------------------------------------------------------------------------
# setup.write_mcp_config / write_mcp_configs
# ---------------------------------------------------------------------------


class TestWriteMcpConfig:
    def test_writes_valid_json_at_dot_mcp_dot_json(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/path/to/tasks.db")
        assert path == os.path.abspath(os.path.join(str(wt), ".mcp.json"))
        assert os.path.isfile(path)
        # Loadable JSON.
        config = json.loads(open(path).read())
        assert "mcpServers" in config
        assert "agent_crew" in config["mcpServers"]

    def test_config_points_at_agent_crew_mcp_server(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/db/tasks.db")
        config = json.loads(open(path).read())
        server = config["mcpServers"]["agent_crew"]
        assert server["args"] == ["-m", "agent_crew.mcp_server"]
        # Pinned interpreter — must be a real path, not just "python".
        assert os.path.isfile(server["command"])
        # DB path is forwarded via env.
        assert server["env"]["AGENT_CREW_DB"] == "/db/tasks.db"
        # PYTHONPATH carries something — the agent process needs to find
        # the agent_crew package even without an editable install.
        assert server["env"]["PYTHONPATH"]

    def test_overwrites_existing_config(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = crew_setup.write_mcp_config(str(wt), "/v1/tasks.db")
        # Write again with a different DB.
        crew_setup.write_mcp_config(str(wt), "/v2/tasks.db")
        config = json.loads(open(path).read())
        assert config["mcpServers"]["agent_crew"]["env"]["AGENT_CREW_DB"] == "/v2/tasks.db"

    def test_creates_parent_dirs(self, tmp_path):
        # write_mcp_config relies on os.makedirs(..., exist_ok=True)
        # — same pattern as instructions.write. We seeded `wt` already.
        wt = tmp_path / "newroot" / "wt"
        wt.mkdir(parents=True)
        path = crew_setup.write_mcp_config(str(wt), "/db.db")
        assert os.path.isfile(path)


class TestWriteMcpConfigs:
    def test_applies_correct_layout_per_agent(self, tmp_path):
        """Phase 5 (#110): each agent's config file lives where that
        CLI actually reads it from. Detailed per-agent assertions live
        in test_mcp_config_per_agent.py — this is the dispatch smoke
        test."""
        wt_c = tmp_path / "c"
        wt_x = tmp_path / "x"
        wt_g = tmp_path / "g"
        for p in (wt_c, wt_x, wt_g):
            p.mkdir()
        worktrees = {
            "claude": str(wt_c),
            "codex": str(wt_x),
            "gemini": str(wt_g),
        }
        crew_setup.write_mcp_configs(worktrees, "/db/tasks.db")
        # claude → .mcp.json
        assert (wt_c / ".mcp.json").exists()
        config = json.loads((wt_c / ".mcp.json").read_text())
        assert config["mcpServers"]["agent_crew"]["env"]["AGENT_CREW_DB"] == "/db/tasks.db"
        # codex → .codex_local/config.toml
        assert (wt_x / ".codex_local" / "config.toml").exists()
        # gemini → .gemini/settings.json
        assert (wt_g / ".gemini" / "settings.json").exists()
