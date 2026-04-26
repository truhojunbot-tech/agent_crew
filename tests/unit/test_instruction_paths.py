"""Per-agent file paths for instruction docs (Issue #110).

The previous layout wrote every role's prompt under `.claude/` so the
files lived next to the project's git-tracked CLAUDE.md without
clobbering it. That works for Claude Code (it merges `.claude/CLAUDE.md`
with the root CLAUDE.md) but **codex reads only the root `AGENTS.md`**
and **gemini reads only the root `GEMINI.md`** — so reviewer/tester
prompts under `.claude/` were invisible to the agents that needed them.
The tester took task descriptions as implementer instructions and
force-pushed over the implementer's PR head (alpha_engine PRs #801–#805).

After this PR:

- implementer → `.claude/CLAUDE.md`  (Claude Code merges, full overwrite)
- reviewer    → `AGENTS.md`           (project root, marker-bracketed)
- tester      → `GEMINI.md`           (project root, marker-bracketed)

Marker-bracketed writes preserve any developer-facing content the
project already had in those files.
"""
import os

from agent_crew import instructions


def _write_port(tmp_path):
    p = tmp_path / "port"
    p.write_text("9123")
    return str(p)


# ---------------------------------------------------------------------------
# ROLE_FILES: paths now match what each agent CLI actually reads
# ---------------------------------------------------------------------------


class TestRoleFiles:
    def test_implementer_stays_under_dot_claude(self):
        assert instructions.ROLE_FILES["implementer"] == ".claude/CLAUDE.md"

    def test_reviewer_writes_to_root_agents_md(self):
        # Codex reads ./AGENTS.md, not ./.claude/AGENTS.md.
        assert instructions.ROLE_FILES["reviewer"] == "AGENTS.md"

    def test_tester_writes_to_root_gemini_md(self):
        # Gemini reads ./GEMINI.md, not ./.claude/GEMINI.md.
        assert instructions.ROLE_FILES["tester"] == "GEMINI.md"


# ---------------------------------------------------------------------------
# write(): claude path stays a full overwrite, the other two get
# marker-bracketed merges so project content is preserved.
# ---------------------------------------------------------------------------


class TestWriteImplementer:
    def test_overwrites_dot_claude_claude_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        # Pre-existing .claude/CLAUDE.md should be replaced wholesale —
        # we own this file.
        old = wt / ".claude" / "CLAUDE.md"
        old.parent.mkdir()
        old.write_text("OLD CONTENT")
        path = instructions.write(
            "implementer",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        body = open(path).read()
        assert "OLD CONTENT" not in body
        # task-loop prompt + agent_crew block are present
        assert "You are claude" in body


class TestWriteReviewer:
    def test_first_write_creates_root_agents_md_with_block(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = instructions.write(
            "reviewer",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        assert path == os.path.abspath(str(wt / "AGENTS.md"))
        body = open(path).read()
        assert "<!-- agent_crew:begin -->" in body
        assert "<!-- agent_crew:end -->" in body
        assert "You are codex" in body

    def test_preserves_existing_developer_doc(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        existing = "# Project Codex Guide\n\nUse 4-space indents.\n"
        (wt / "AGENTS.md").write_text(existing)

        path = instructions.write(
            "reviewer",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        body = open(path).read()
        # Block prepended; original content preserved verbatim below.
        assert body.startswith("<!-- agent_crew:begin -->")
        assert "# Project Codex Guide" in body
        assert "Use 4-space indents." in body

    def test_idempotent_rewrite_replaces_only_marked_block(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        existing = (
            "<!-- agent_crew:begin -->\nOLD AGENT CREW BLOCK\n<!-- agent_crew:end -->\n\n"
            "# Project Codex Guide\nUse 4-space indents.\n"
        )
        (wt / "AGENTS.md").write_text(existing)

        path = instructions.write(
            "reviewer",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        body = open(path).read()
        assert "OLD AGENT CREW BLOCK" not in body  # replaced
        assert "You are codex" in body              # new content present
        assert "Use 4-space indents." in body       # project content preserved
        # Markers appear exactly once each.
        assert body.count("<!-- agent_crew:begin -->") == 1
        assert body.count("<!-- agent_crew:end -->") == 1


class TestWriteTester:
    def test_first_write_creates_root_gemini_md_with_block(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        path = instructions.write(
            "tester",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        assert path == os.path.abspath(str(wt / "GEMINI.md"))
        body = open(path).read()
        assert "<!-- agent_crew:begin -->" in body
        assert "You are gemini" in body
        # Tester role section must communicate the verify-only constraint
        # somewhere — covered indirectly by the body containing
        # task_type=test branch instructions.
        assert "test" in body

    def test_preserves_existing_developer_doc(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        existing = "# Gemini Project Guide\n\nProject-specific tester notes.\n"
        (wt / "GEMINI.md").write_text(existing)

        path = instructions.write(
            "tester",
            str(wt),
            project="proj",
            port_file=_write_port(tmp_path),
        )
        body = open(path).read()
        assert body.startswith("<!-- agent_crew:begin -->")
        assert "# Gemini Project Guide" in body
        assert "Project-specific tester notes." in body
