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
# AGENT_FILES: paths match what each agent CLI actually reads
# ---------------------------------------------------------------------------


class TestAgentFiles:
    def test_claude_reads_dot_claude(self):
        assert instructions.AGENT_FILES["claude"] == ".claude/CLAUDE.md"

    def test_codex_reads_root_agents_md(self):
        # Codex reads ./AGENTS.md, not ./.claude/AGENTS.md.
        assert instructions.AGENT_FILES["codex"] == "AGENTS.md"

    def test_gemini_reads_root_gemini_md(self):
        # Gemini reads ./GEMINI.md, not ./.claude/GEMINI.md.
        assert instructions.AGENT_FILES["gemini"] == "GEMINI.md"


# ---------------------------------------------------------------------------
# write(): claude path stays a full overwrite, the other two get
# marker-bracketed merges so project content is preserved.
# ---------------------------------------------------------------------------


class TestWriteImplementer:
    def test_overwrites_dot_claude_claude_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        # Legacy setup emitted an unmarked agent_crew contract; replace it.
        old = wt / ".claude" / "CLAUDE.md"
        old.parent.mkdir()
        old.write_text(instructions.generate("reviewer", "proj", 9123, agent="claude"))
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
        assert "## Role: implementer" in body
        assert "## Role: reviewer" not in body


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


class TestAgentSelectedProtocolFiles:
    def test_rewrite_twice_preserves_developer_content_outside_markers(self, tmp_path):
        """A normal post-task rewrite must not eat project documentation."""
        developer_doc = "# Project guide\nMINE: project-specific rule\n"
        # Each provider's own filename is rewritten after a dispatcher task.
        for role, agent, filename in (
            ("reviewer", "claude", ".claude/CLAUDE.md"),
            ("implementer", "codex", "AGENTS.md"),
            ("tester", "gemini", "GEMINI.md"),
        ):
            wt = tmp_path / agent
            wt.mkdir()
            target = wt / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(developer_doc)

            for _ in range(2):
                instructions.write(
                    role, str(wt), project="proj", port_file=_write_port(tmp_path), agent=agent,
                )

            body = target.read_text()
            assert developer_doc in body
            assert body.count("<!-- agent_crew:begin -->") == 1

    def test_developer_doc_quoting_legacy_signals_is_not_replaced(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        developer_doc = (
            "# Team notes\n"
            "We quote '# Agent Crew — example' in this document.\n"
            "OVERRIDE: You are an agent_crew worker is a quoted policy line.\n"
            "## Role: documentation\n"
        )
        target = wt / "AGENTS.md"
        target.write_text(developer_doc)

        instructions.write(
            "implementer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="codex",
        )

        assert developer_doc in target.read_text()

    def test_lightly_edited_legacy_contract_is_preserved_as_developer_content(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        target = wt / ".claude" / "CLAUDE.md"
        target.parent.mkdir()
        developer_rule = "\n## My extra rule\nNever touch production.\n"
        target.write_text(
            instructions.generate("implementer", "proj", 9123, agent="claude") + developer_rule
        )

        instructions.write(
            "reviewer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="claude",
        )

        assert developer_rule in target.read_text()

    def test_developer_claude_doc_survives_stale_cleanup(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        stale = wt / ".claude" / "CLAUDE.md"
        stale.parent.mkdir()
        stale.write_text("developer Claude rules\n")
        instructions.write("tester", str(wt), project="proj", port_file=_write_port(tmp_path), agent="gemini")
        assert stale.read_text() == "developer Claude rules\n"
    def test_codex_implementer_receives_push_contract_in_agents_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()

        path = instructions.write(
            "implementer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="codex",
        )

        assert path == os.path.abspath(str(wt / "AGENTS.md"))
        body = (wt / "AGENTS.md").read_text()
        assert "## Role: implementer" in body
        assert "git push origin HEAD:<branch-name>" in body

    def test_claude_reviewer_receives_review_contract_in_claude_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()

        path = instructions.write(
            "reviewer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="claude",
        )

        assert path == os.path.abspath(str(wt / ".claude" / "CLAUDE.md"))
        body = (wt / ".claude" / "CLAUDE.md").read_text()
        assert "## Role: reviewer" in body
        assert "git push origin HEAD:<branch-name>" not in body

    def test_agent_write_removes_stale_legacy_role_protocol_file(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        stale = wt / ".claude" / "CLAUDE.md"
        stale.parent.mkdir()
        stale.write_text(instructions.generate("implementer", "proj", 9123, agent="claude"))

        instructions.write(
            "implementer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="codex",
        )

        assert (wt / "AGENTS.md").exists()
        assert not stale.exists()

    def test_codex_contract_preserves_developer_agents_doc(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        developer_doc = "# Project guide\nDo not overwrite this.\n"
        (wt / "AGENTS.md").write_text(developer_doc)

        instructions.write(
            "implementer", str(wt), project="proj", port_file=_write_port(tmp_path), agent="codex",
        )

        body = (wt / "AGENTS.md").read_text()
        assert developer_doc in body
        assert "## Role: implementer" in body
