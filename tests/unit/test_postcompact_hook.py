"""PostCompact hook for Claude worktrees (Issue #122).

After ``/compact`` Claude Code drops the long task-loop system prompt
that was injected at session start. Without re-injection the agent has
no rule telling it to call `get_next_task` again, so the pull loop
stalls until the operator manually re-prompts.

The fix is a per-worktree ``.claude/settings.local.json`` PostCompact
hook that emits ``additionalContext`` containing the compact restoration
prompt from ``agent_crew.prompts.task_loop.build_task_loop_prompt_compact``.
This file pins the contract:

  - the file gets written when claude's MCP config is set up
  - the hook command uses the same interpreter+PYTHONPATH as the MCP
    config (so it works in the same env)
  - executing the hook command produces well-formed JSON that Claude
    Code can ingest, with ``hookSpecificOutput.additionalContext``
    containing the compact prompt for the right agent/role

For codex and gemini we don't know an equivalent compact-recovery
surface yet — explicitly out of scope for this issue. Tracked as a
follow-up if a new failure mode shows up.
"""
import json
import subprocess

from agent_crew import setup as crew_setup


class TestPostCompactHookFileWritten:
    def test_settings_local_json_created_for_claude(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="claude")
        path = wt / ".claude" / "settings.local.json"
        assert path.exists(), (
            f"expected PostCompact hook at {path}, but it wasn't created"
        )
        config = json.loads(path.read_text())
        assert "hooks" in config
        assert "PostCompact" in config["hooks"]

    def test_postcompact_hook_has_command_entry(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="claude")
        config = json.loads(
            (wt / ".claude" / "settings.local.json").read_text()
        )
        post = config["hooks"]["PostCompact"]
        assert isinstance(post, list) and post, "PostCompact must be a non-empty list"
        # Each block has hooks: [{type: command, command: "...", timeout: N}]
        inner = post[0]["hooks"]
        assert any(h.get("type") == "command" and h.get("command") for h in inner)

    def test_codex_does_not_install_postcompact_hook(self, tmp_path):
        """Codex has no equivalent hook surface — we don't pretend to wire one."""
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="codex")
        # The Claude-specific settings file must not be created for a
        # non-claude worktree — that path belongs to claude only.
        assert not (wt / ".claude" / "settings.local.json").exists()

    def test_gemini_does_not_install_postcompact_hook(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="gemini")
        assert not (wt / ".claude" / "settings.local.json").exists()


class TestPostCompactHookExecutes:
    """Run the hook command end-to-end and parse the JSON it emits."""

    def test_command_prints_well_formed_additional_context(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="claude")
        config = json.loads(
            (wt / ".claude" / "settings.local.json").read_text()
        )
        cmd = config["hooks"]["PostCompact"][0]["hooks"][0]["command"]

        # Run the hook the same way Claude Code would: as a shell command.
        # The command bakes in PYTHONPATH so we don't need to set env here.
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"hook command failed: stderr={result.stderr}"
        )
        payload = json.loads(result.stdout)
        # Claude Code's PostCompact hook contract:
        out = payload["hookSpecificOutput"]
        assert out["hookEventName"] == "PostCompact"
        ctx = out["additionalContext"]
        # The compact prompt mentions the loop and the right agent.
        assert "get_next_task" in ctx
        assert "claude" in ctx
        assert "submit_result" in ctx

    def test_command_targets_implementer_role_for_claude(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        crew_setup.write_mcp_config(str(wt), "/db.db", agent="claude")
        config = json.loads(
            (wt / ".claude" / "settings.local.json").read_text()
        )
        cmd = config["hooks"]["PostCompact"][0]["hooks"][0]["command"]
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        # role mention — full prompt format is "default role: implementer"
        assert "implementer" in ctx
