"""#288 — a measurement nobody can read is not telemetry.

#285 added `claude_context_tokens()` and put `context_tokens` in the cap-info
dict, and I said in that PR that "the measurement ships on regardless" so the
quota layer could pick a threshold from a week of real data. That was wrong, and
the reporter is right about why: the only durable sink was
`provider_context_capped`, emitted under `if _ctx_over:`. With the token cap off
by default (#284) and every store under the 64 MB byte cap, the number was
computed on every dispatch and then dropped.

So the fleet's measured 500k–600k windows produced no rows at all, and the
decision #284 deferred to the quota layer had nothing to stand on.

What this pins:

  * a distinct `provider_context_observed` event — ⛔`provider_context_capped`
    keeps its meaning, which is "a reset was forced". Overloading it to mean
    "here is a number" would corrupt the one signal that already works;
  * `null` for unknown, `0` for a measured zero. These are different facts and a
    cohort built on the wrong one is wrong in a way nobody can see afterwards;
  * exactly one row per dispatch — observed XOR capped, never both.
"""

import asyncio
import json
import os
import re

import pytest

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

CWD_SUFFIX = "worktrees/demo/claude"


def _usage(cache_read=0, cache_creation=0, input_tokens=0):
    return {"input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": 7}


def _session(home, cwd, usage_lines, name="sess-288"):
    d = home / "projects" / re.sub(r"[/._]", "-", str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    with open(f, "w") as fh:
        for u in usage_lines:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": u}}) + "\n")
    return f


# ── 1. unknown and zero stop being the same value ─────────────────────


def test_a_measured_zero_is_zero(tmp_path):
    _session(tmp_path, "/w/claude", [_usage(cache_read=0, cache_creation=0,
                                            input_tokens=0)])
    assert sv.claude_context_tokens("/w/claude", home=tmp_path)[0] == 0


def test_a_real_window_is_the_number(tmp_path):
    _session(tmp_path, "/w/claude", [_usage(cache_read=601_674, cache_creation=5_026,
                                            input_tokens=2)])
    assert sv.claude_context_tokens("/w/claude", home=tmp_path)[0] == 606_702


@pytest.mark.parametrize("lines", [[], [{}], [{"output_tokens": 5}]])
def test_no_usage_to_read_is_unknown_not_zero(tmp_path, lines):
    """★★The distinction #288 turns on. Returning `0` here — as #285 did — makes
    an unmeasurable session indistinguishable from an empty one, and a cohort
    built on that is wrong in a way nobody can detect afterwards."""
    _session(tmp_path, "/w/claude", lines)
    assert sv.claude_context_tokens("/w/claude", home=tmp_path)[0] is None


def test_a_missing_store_is_unknown(tmp_path):
    assert sv.claude_context_tokens("/w/nothing", home=tmp_path) == (None, "")


def test_unknown_never_trips_a_cap(tmp_path):
    """⛔`None` must not compare as small OR as large. A cap that fires on an
    unmeasurable session forces resets for a reading nobody took."""
    _session(tmp_path, "/w/claude", [{}])
    over, info = sv.claude_context_exceeds_cap("/w/claude", home=tmp_path,
                                               max_tokens=1)
    assert over is False and info["context_tokens"] is None


def test_a_measured_zero_never_trips_a_cap(tmp_path):
    _session(tmp_path, "/w/claude", [_usage()])
    over, info = sv.claude_context_exceeds_cap("/w/claude", home=tmp_path,
                                               max_tokens=1)
    assert over is False and info["context_tokens"] == 0


# ── 2. the durable row ────────────────────────────────────────────────


def _dispatch(tmp_path, monkeypatch, *, agent="claude", cap_info=None, over=False):
    """One real dispatch; returns the lifecycle events it wrote."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    wt = tmp_path / "worktrees" / "demo" / agent
    wt.mkdir(parents=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {agent: str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    for name in ("agy_context_exceeds_cap", "codex_context_exceeds_cap",
                 "claude_context_exceeds_cap"):
        monkeypatch.setattr(sv, name, lambda *a, **k: (False, {}))
    target = {"claude": "claude_context_exceeds_cap",
              "codex": "codex_context_exceeds_cap",
              "gemini": "agy_context_exceeds_cap"}[agent]
    monkeypatch.setattr(sv, target, lambda *a, **k: (over, dict(cap_info or {})))

    role = {"claude": "implementer", "codex": "reviewer", "gemini": "tester"}[agent]
    task_type = {"implementer": "implement", "reviewer": "review",
                 "tester": "test"}[role]
    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="t-288", task_type=task_type, description="go",
                              branch="main", context={}))
        task = q.dequeue(role=role)
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, role))

    path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path)]


def _of(events, kind):
    return [e for e in events if e.get("event_type") == kind]


CLAUDE_INFO = {"provider": "claude", "bytes": 9_781_828,
               "conversation_id": "627091a5", "cap_mb": 64.0,
               "context_tokens": 606_702, "cap_tokens": 0, "tripped_by": ""}


def test_a_normal_dispatch_now_leaves_a_row(tmp_path, monkeypatch):
    """★★The bug. Before #288 an uncapped dispatch wrote nothing at all."""
    events = _dispatch(tmp_path, monkeypatch, cap_info=CLAUDE_INFO)
    observed = _of(events, "provider_context_observed")
    assert len(observed) == 1, [e.get("event_type") for e in events]
    assert observed[0]["context_tokens"] == 606_702


def test_the_row_carries_what_a_cohort_needs_to_join_on(tmp_path, monkeypatch):
    """The issue lists these by name: without them the observation cannot be
    attached to task economics and is a number in a file."""
    event = _of(_dispatch(tmp_path, monkeypatch, cap_info=CLAUDE_INFO),
                "provider_context_observed")[0]
    assert event["task_id"] == "t-288"
    assert event["provider"] == "claude"
    assert event["context_bytes"] == 9_781_828
    assert event["provider_session_id"] == "627091a5"
    assert event["context_id"] and isinstance(event["context_generation"], int)


def test_a_measured_zero_is_emitted_as_zero(tmp_path, monkeypatch):
    event = _of(_dispatch(tmp_path, monkeypatch,
                          cap_info={**CLAUDE_INFO, "context_tokens": 0}),
                "provider_context_observed")[0]
    assert event["context_tokens"] == 0


def test_an_unknown_measurement_is_emitted_as_null(tmp_path, monkeypatch):
    """⛔Emitted, not skipped. "We looked and could not tell" is itself a fact
    the cohort needs — dropping the row would make unknowns invisible and bias
    the sample toward sessions that happen to be readable."""
    event = _of(_dispatch(tmp_path, monkeypatch,
                          cap_info={**CLAUDE_INFO, "context_tokens": None}),
                "provider_context_observed")[0]
    assert "context_tokens" in event and event["context_tokens"] is None


def test_a_capped_dispatch_is_not_also_observed(tmp_path, monkeypatch):
    """★★No double counting. The cap event already carries the numbers, and two
    rows for one dispatch would inflate any per-dispatch aggregate."""
    events = _dispatch(tmp_path, monkeypatch, over=True,
                       cap_info={**CLAUDE_INFO, "cap_tokens": 400_000,
                                 "tripped_by": "tokens"})
    assert len(_of(events, "provider_context_capped")) == 1
    assert _of(events, "provider_context_observed") == []


def test_the_cap_event_keeps_its_own_meaning(tmp_path, monkeypatch):
    """⛔`provider_context_capped` means a reset was forced. #288 explicitly
    asks not to overload it, so an uncapped dispatch must never emit one."""
    events = _dispatch(tmp_path, monkeypatch, cap_info=CLAUDE_INFO)
    assert _of(events, "provider_context_capped") == []


@pytest.mark.parametrize("agent, info", [
    ("gemini", {"provider": "agy", "bytes": 20_874_035, "conversation_id": "0aff70cb"}),
    ("codex", {"provider": "codex", "bytes": 6_617_088, "conversation_id": "01a02294"}),
])
def test_the_other_providers_are_observed_too(tmp_path, monkeypatch, agent, info):
    """⛔Tokens are a Claude-only measurement today, so theirs is `null` — which
    is the honest value, not a reason to leave them out of the stream. A cohort
    that can only see one provider cannot compare policies across them."""
    event = _of(_dispatch(tmp_path, monkeypatch, agent=agent, cap_info=info),
                "provider_context_observed")[0]
    assert event["provider"] == info["provider"]
    assert event["context_bytes"] == info["bytes"]
    assert event["context_tokens"] is None


def test_nothing_is_emitted_when_no_measurement_was_attempted(tmp_path, monkeypatch):
    """⛔"Not measured" and "measured, unknown" are different. An agent with no
    sizing support at all should leave no row, or the stream would imply an
    attempt that never happened."""
    events = _dispatch(tmp_path, monkeypatch, cap_info={})
    assert _of(events, "provider_context_observed") == []
