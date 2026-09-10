"""#284 — the byte cap cannot see the cost shape it was built to stop.

#260/#261 capped the Claude session by file size. quota-ops reported the gap:
its review worker's session had run unrotated since 2026-08-21, and on
2026-09-10 its last turn re-billed **601,674** `cache_read_input_tokens` — from
a **9.33 MB** file, comfortably under the 64 MB cap, so `over=False` forever.

Verified on this host before writing anything, and every reported number holds:

    9.33 MB   first 2026-08-21T04:31:37Z   cache_read_input_tokens: 601674

Measuring the whole fleet's claude worktrees the same way shows the two signals
are close to uncorrelated, which is the substance of the finding:

    project              MB      window≈
    agent_council      0.13       62,025
    halla              0.97      231,590
    quota-core         3.61      575,398
    quota-ops          9.33      606,702
    agent_crew        17.18      517,710
    alpha_engine      32.53      363,824

⛔quota-ops carries a LARGER window than alpha_engine from a file a third the
  size. Not one of the seven is near the 64 MB cap, yet four re-bill over 350k
  tokens on every turn. A byte cap cannot rank these at all.

What this pins: the measurement, its cheapness (tail read, not a full scan),
and that it is reported even when no token cap is set — because choosing the
threshold is the quota layer's call, not this one's.
"""

import json
import os
import re

import pytest

from agent_crew import server as sv

CWD = "/home/u/.agent_crew/worktrees/quota-ops/claude"


def _session(home, cwd, name="627091a5", *, usage_lines, pad=0):
    d = home / "projects" / re.sub(r"[/._]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    with open(f, "w") as fh:
        if pad:
            # a body that dwarfs the tail, so a full-file scan is measurable
            filler = json.dumps({"type": "user", "message": {"content": "x" * 512}})
            for _ in range(pad):
                fh.write(filler + "\n")
        for u in usage_lines:
            fh.write(json.dumps(
                {"type": "assistant", "message": {"usage": u}}) + "\n")
    return f


def _usage(cache_read=0, cache_creation=0, input_tokens=0):
    return {"input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": 1219}


# ── 1. reading the last turn's usage ──────────────────────────────────


def test_the_reported_window_is_read_from_the_last_turn(tmp_path):
    """★★The quota-ops numbers, reconstructed."""
    _session(tmp_path, CWD, usage_lines=[
        _usage(cache_read=120_000),
        _usage(cache_read=601_674, cache_creation=5_026, input_tokens=2),
    ])
    tokens, session = sv.claude_context_tokens(CWD, home=tmp_path)
    assert tokens == 601_674 + 5_026 + 2
    assert session == "627091a5"


def test_output_tokens_are_not_part_of_the_window():
    """⛔`output_tokens` is what the turn produced, not what it re-reads. Adding
    it would inflate the number the cap is compared against."""
    assert sv._usage_context_tokens(_usage(cache_read=10, cache_creation=5,
                                           input_tokens=1)) == 16


def test_a_turn_without_usage_is_skipped(tmp_path):
    """Tool results and user turns carry no usage block; the last one that does
    is the live window."""
    d = tmp_path / "projects" / re.sub(r"[/._]", "-", CWD)
    d.mkdir(parents=True)
    with open(d / "s.jsonl", "w") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "message": {"usage": _usage(cache_read=444)}}) + "\n")
        fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        fh.write(json.dumps({"type": "tool_result", "content": "ok"}) + "\n")
    assert sv.claude_context_tokens(CWD, home=tmp_path)[0] == 444


@pytest.mark.parametrize("body", ["", "not json\n", '{"message":{}}\n'])
def test_an_unreadable_session_reports_unknown_not_zero_pretending(tmp_path, body):
    """⛔Sizing must never break a dispatch, and it must not invent a small
    number either — 0 here means "no measurement", and the caller treats a
    0 as not-over rather than as a healthy session."""
    d = tmp_path / "projects" / re.sub(r"[/._]", "-", CWD)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(body)
    assert sv.claude_context_tokens(CWD, home=tmp_path)[0] == 0


def test_a_missing_worktree_is_not_an_error(tmp_path):
    assert sv.claude_context_tokens("/nope/nothing", home=tmp_path) == (0, "")


def test_the_last_turn_is_found_without_reading_the_whole_file(tmp_path):
    """⛔This runs on every dispatch, against files that reached 365 MB on this
    host (#269). A full scan to find the last line would put that cost in the
    dispatch path, so the read walks backwards from the end."""
    f = _session(tmp_path, CWD, usage_lines=[_usage(cache_read=777)], pad=20_000)
    assert os.path.getsize(f) > 10 * 1024 * 1024

    reads = []
    real_open = open

    class _CountingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            reads.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    def counting_open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        return _CountingFile(fh) if "b" in mode else fh

    import builtins
    builtins.open = counting_open
    try:
        assert sv.claude_context_tokens(CWD, home=tmp_path)[0] == 777
    finally:
        builtins.open = real_open
    assert sum(reads) < os.path.getsize(f) / 2, \
        f"read {sum(reads)} bytes of a {os.path.getsize(f)}-byte file"


# ── 2. the cap gains a second dimension ───────────────────────────────


def test_the_reported_session_is_still_under_the_byte_cap(tmp_path):
    """⛔The control that makes the whole issue legible: this session is NOT
    oversized on disk, and must not be reported as if it were."""
    _session(tmp_path, CWD, usage_lines=[_usage(cache_read=601_674)])
    over, info = sv.claude_context_exceeds_cap(CWD, home=tmp_path,
                                               max_tokens=0)
    assert over is False
    assert info["bytes"] < 64 * 1048576


def test_a_token_cap_catches_what_the_byte_cap_cannot(tmp_path):
    """★★The finding: a compact file with a large window."""
    _session(tmp_path, CWD, usage_lines=[_usage(cache_read=601_674)])
    over, info = sv.claude_context_exceeds_cap(CWD, home=tmp_path,
                                               max_tokens=400_000)
    assert over is True
    assert info["context_tokens"] == 601_674
    assert info["cap_tokens"] == 400_000


def test_the_info_says_WHICH_signal_tripped(tmp_path):
    """Two dimensions means a reader has to be able to tell them apart —
    otherwise the reset event cannot be attributed to a cause."""
    _session(tmp_path, CWD, usage_lines=[_usage(cache_read=601_674)])
    assert sv.claude_context_exceeds_cap(CWD, home=tmp_path,
                                         max_tokens=400_000)[1]["tripped_by"] == "tokens"

    big = _session(tmp_path, CWD, name="fat", usage_lines=[_usage(cache_read=10)])
    with open(big, "a") as fh:
        fh.write("x" * (3 * 1048576))
    over, info = sv.claude_context_exceeds_cap(CWD, max_mb=1, max_tokens=0,
                                               home=tmp_path)
    assert over is True and info["tripped_by"] == "bytes"


def test_the_window_is_reported_even_when_no_token_cap_is_set(tmp_path):
    """★★The part that ships value on day one. Choosing a fleet-wide reset
    threshold is the quota layer's decision, so the default is OFF — but the
    measurement is recorded regardless, or that decision has no data to stand
    on and #284 gets rediscovered later."""
    _session(tmp_path, CWD, usage_lines=[_usage(cache_read=601_674)])
    over, info = sv.claude_context_exceeds_cap(CWD, home=tmp_path, max_tokens=0)
    assert over is False
    assert info["context_tokens"] == 601_674, "the measurement was dropped"


def test_the_default_token_cap_is_off(tmp_path):
    """⛔Deliberate. On the measured fleet a 400k default would reset three of
    seven worktrees on their next dispatch, and alpha_engine reached 363k
    shortly after a rotation — so a low cap would thrash. Picking that number
    is provider-economics policy, which this layer does not own."""
    assert sv.CLAUDE_CONTEXT_MAX_TOKENS == 0


@pytest.mark.parametrize("tokens, cap, expected", [
    (601_674, 400_000, True),
    (400_000, 400_000, False),      # a cap is a ceiling, not a trigger
    (400_001, 400_000, True),
    (0, 400_000, False),            # unmeasurable is not over
    (601_674, 0, False),            # disabled
    (601_674, -1, False),
])
def test_the_token_comparison(tmp_path, tokens, cap, expected):
    _session(tmp_path, CWD, usage_lines=[_usage(cache_read=tokens)] if tokens else [])
    assert sv.claude_context_exceeds_cap(CWD, home=tmp_path,
                                         max_tokens=cap)[0] is expected


def test_the_byte_cap_still_works_unchanged(tmp_path):
    """⛔Regression guard for #260/#261. The new dimension is OR-ed on; it must
    not weaken the one that already catches alpha_engine's shape."""
    f = _session(tmp_path, CWD, usage_lines=[_usage(cache_read=10)])
    with open(f, "a") as fh:
        fh.write("x" * (3 * 1048576))
    assert sv.claude_context_exceeds_cap(CWD, max_mb=1, max_tokens=0,
                                         home=tmp_path)[0] is True
    assert sv.claude_context_exceeds_cap(CWD, max_mb=64, max_tokens=0,
                                         home=tmp_path)[0] is False


# ── 3. the reset event has to say why ─────────────────────────────────


def test_the_cap_event_carries_the_window_and_the_cause(tmp_path, monkeypatch):
    """★★A reset that does not say which signal fired cannot be attributed, and
    on this fleet the two signals disagree about which worktrees are expensive.
    Byte-only telemetry would keep the #284 blind spot in the reset record."""
    import asyncio
    import json as _json

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True)
    state = tmp_path / "state.json"
    state.write_text(_json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "agy_context_exceeds_cap", lambda cwd, *a, **k: (False, {}))
    monkeypatch.setattr(
        sv, "claude_context_exceeds_cap",
        lambda cwd, *a, **k: (True, {"bytes": 9_781_828, "conversation_id": "627091a5",
                                     "cap_mb": 64.0, "context_tokens": 606_702,
                                     "cap_tokens": 400_000, "tripped_by": "tokens",
                                     "provider": "claude"}))

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="impl-284", task_type="implement",
                              description="go", branch="main", context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    events = [json.loads(line) for line in open(path)]
    capped = [e for e in events if e.get("event_type") == "provider_context_capped"]
    assert capped, "no cap event was emitted"
    assert capped[0]["tripped_by"] == "tokens"
    assert capped[0]["context_tokens"] == 606_702
    assert capped[0]["cap_tokens"] == 400_000
    assert capped[0]["bytes"] == 9_781_828, "the byte figure was dropped"
