import os

ROLE_FILES: dict = {
    "implementer": "CLAUDE.md",
    "reviewer": "AGENTS.md",
    "tester": "GEMINI.md",
}

_COMMON = """\
# Agent Crew — <project>

## Task Queue (HTTP)
- Receive next task: GET http://localhost:<port>/tasks/next
- Submit result:     POST http://localhost:<port>/tasks/{id}/result

## Common Instructions
- Follow TDD: write tests first, then implement
- Commit and push when done
"""

_ROLE_SECTIONS: dict = {
    "implementer": """\
## Role: implementer
- Write production code in src/
- Run pytest and fix failures before committing
- Branch: agent/claude
""",
    "reviewer": """\
## Role: reviewer
- Review diffs and open GitHub PRs
- Leave structured feedback in result.md
- Do not merge without approval gate
""",
    "tester": """\
## Role: tester
- Write and maintain tests in tests/
- Ensure full coverage of new code paths
- Report flaky tests immediately
""",
}


def generate(role: str, project: str, port: int) -> str:
    section = _ROLE_SECTIONS.get(role, f"## Role: {role}\n")
    content = (_COMMON + section).replace("<project>", project).replace("<port>", str(port))
    return content


def write(role: str, worktree_path: str, project: str, port_file: str) -> str:
    if role not in ROLE_FILES:
        raise ValueError(f"Unknown role: {role!r}. Must be one of {list(ROLE_FILES)}")
    with open(port_file) as f:
        port = int(f.read().strip())
    filename = ROLE_FILES[role]
    content = generate(role, project, port)
    path = os.path.join(worktree_path, filename)
    with open(path, "w") as f:
        f.write(content)
    return os.path.abspath(path)
