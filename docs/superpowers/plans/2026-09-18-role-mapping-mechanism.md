# Explicit Project Role Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, provider-neutral project role mapping CLI without changing legacy or STOP/HOLD behavior.

**Architecture:** Store new operator-selected mappings under `explicit_role_to_agent` in project `state.json`. A single resolver returns both the effective mapping and its provenance, with precedence explicit mapping, legacy `roles` entries, then current hardcoded defaults. The server uses that resolver at startup; a `crew roles` command group writes or reports only the new field.

**Tech Stack:** Python 3.10, Click, JSON state files, pytest.

---

### Task 1: Define and test effective mapping resolution

**Files:**
- Modify: `src/agent_crew/server.py:739-1490`
- Test: `tests/unit/test_role_mapping.py`

- [x] **Step 1: Write failing resolver tests**

```python
assert resolve_role_mapping({"explicit_role_to_agent": {
    "implementer": "codex", "reviewer": "claude", "tester": "gemini",
}}) == ({"implementer": "codex", "reviewer": "claude", "tester": "gemini"}, "explicit project config")
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_role_mapping.py -q`
Expected: FAIL because the resolver does not exist.

- [x] **Step 3: Implement the pure resolver and load integration**

```python
def resolve_role_mapping(state: dict | None) -> tuple[dict[str, str], str]:
    if state and state.get("explicit_role_to_agent"):
        return dict(state["explicit_role_to_agent"]), "explicit project config"
    if state and state.get("roles"):
        result = dict(_DEFAULT_ROLE_TO_AGENT)
        for item in state["roles"]:
            if item.get("role") and item.get("agent"):
                result[item["role"]] = item["agent"]
        return result, "legacy state.json.roles"
    return dict(_DEFAULT_ROLE_TO_AGENT), "hardcoded default"
```

- [x] **Step 4: Run resolver tests to verify they pass**

Run: `pytest tests/unit/test_role_mapping.py -q`
Expected: PASS.

### Task 2: Add durable read/write CLI surface

**Files:**
- Modify: `src/agent_crew/cli.py:60-71,595-605`
- Test: `tests/unit/test_role_mapping.py`

- [x] **Step 1: Write failing Click command tests**

```python
result = runner.invoke(crew, ["roles", "set", "demo", "implementer=codex", "reviewer=claude", "tester=gemini", "--base", str(tmp_path)])
assert result.exit_code == 0
assert json.loads(state_path.read_text())["explicit_role_to_agent"]["implementer"] == "codex"
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_role_mapping.py -q`
Expected: FAIL because `roles` is not a command.

- [x] **Step 3: Implement `roles set` and `roles show`**

```python
@crew.group()
def roles():
    """Manage durable project role-to-agent mappings."""

@roles.command("set")
def roles_set(project: str, assignments: tuple[str, ...], base: str):
    state = _read_state(base, project) or {"project": project}
    state["explicit_role_to_agent"] = parsed_assignments
    _write_state(base, project, state)
```

- [x] **Step 4: Run CLI tests to verify they pass**

Run: `pytest tests/unit/test_role_mapping.py -q`
Expected: PASS.

### Task 3: Verify compatibility and package regressions

**Files:**
- Test: `tests/unit/test_role_mapping.py`, `tests/test_pause_stop.py`, `tests/test_pause_cascade.py`, `tests/test_stop_epoch.py`

- [x] **Step 1: Add legacy/default status-output regression tests**

```python
assert "Source: legacy state.json.roles" in legacy_show.output
assert "Source: hardcoded default" in default_show.output
```

- [x] **Step 2: Run focused mapping and STOP/pause tests**

Run: `pytest tests/unit/test_role_mapping.py tests/test_pause_stop.py tests/test_pause_cascade.py tests/test_stop_epoch.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full suite and inspect the diff**

Run: `pytest -q && git diff --check && git diff -- src/agent_crew/server.py src/agent_crew/cli.py tests/unit/test_role_mapping.py`
Expected: PASS with no STOP-related production files changed.
