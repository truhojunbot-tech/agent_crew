"""#248 — a dispatcher must be able to say which build it is running.

#247 found that GitHub `main` carried #238 while all four live servers were
still importing an older checkout, so the merged fix had never executed. The
gap was invisible because "the server is up" and "the server is up on the
merged code" were separate questions and only the first had an answer.

The tests that matter here are the ones that stop this tool from recreating the
bug it exists to catch:

  * a report must describe the code the process LOADED, not the checkout at the
    moment you ask — otherwise a later `git pull` makes a stale server look
    current, which is exactly #247 with extra steps;
  * "I cannot tell" must stay distinguishable from "current";
  * nothing here may pull, restart or repair anything.
"""

import json
import os
import subprocess

import pytest

from agent_crew import provenance as prov


def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path, content="VERSION = 1\n"):
    """A throwaway checkout shaped like the real one: <repo>/src/agent_crew."""
    repo = tmp_path / "checkout"
    pkg = repo / "src" / "agent_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(content)
    _run("git", "init", "-q", ".", cwd=repo)
    _run("git", "config", "user.email", "t@example.com", cwd=repo)
    _run("git", "config", "user.name", "t", cwd=repo)
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "first", cwd=repo)
    return repo, pkg


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


# ── 1. it describes THIS process's code ───────────────────────────────


def test_capture_reports_the_checkout_it_is_pointed_at(tmp_path):
    repo, pkg = _repo(tmp_path)

    b = prov.capture(str(pkg))

    assert b["commit"] == _head(repo)
    assert b["ref"] in ("main", "master")
    assert b["dirty"] is False
    assert b["source_file_count"] == 1
    assert b["code_fingerprint"]
    assert b["pid"] == os.getpid()
    assert b["started_at"] <= b["captured_at"]


def test_build_is_frozen_after_the_first_call(monkeypatch, tmp_path):
    """`build()` is a fact about the process, not a live query."""
    monkeypatch.setattr(prov, "_BUILD", None)
    first = prov.build()
    second = prov.build()

    assert first == second
    assert first is not second, "callers must not be able to mutate the frozen build"


# ── 2. ★the regression this whole issue is about ──────────────────────


def test_a_later_git_pull_does_not_make_a_stale_process_look_current(tmp_path):
    """★A process reports the build it LOADED, not the checkout as it is now.

    This is #247 in miniature: the server keeps executing the old bytes while
    someone advances the checkout. A `git rev-parse HEAD` at request time would
    answer with the NEW sha and call the stale runtime current — the exact
    mistake that let a merged fix sit undeployed while being reported as live.
    """
    repo, pkg = _repo(tmp_path)
    loaded = prov.capture(str(pkg))          # the process starts here...

    (pkg / "__init__.py").write_text("VERSION = 2\n")   # ...someone deploys
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "the fix", cwd=repo)
    new_head = _head(repo)

    snap = prov.snapshot(project="p", build_info=loaded)

    assert snap["commit"] == loaded["commit"], "the frozen build moved — it must not"
    assert snap["commit"] != new_head
    assert snap["checkout_commit"] == new_head
    assert snap["checkout_moved_since_start"] is True
    assert snap["source_changed_since_start"] is True


def test_source_change_alone_marks_the_runtime_stale(tmp_path):
    """An uncommitted edit still means the loaded bytes are gone from disk."""
    repo, pkg = _repo(tmp_path)
    loaded = prov.capture(str(pkg))

    (pkg / "__init__.py").write_text("VERSION = 99\n")   # not committed

    snap = prov.snapshot(build_info=loaded)
    assert snap["checkout_moved_since_start"] is False
    assert snap["source_changed_since_start"] is True


def test_an_identical_rewrite_is_not_a_change(tmp_path):
    """Content-addressed, not mtime-based: a no-op pull is not a false alarm."""
    repo, pkg = _repo(tmp_path)
    loaded = prov.capture(str(pkg))

    (pkg / "__init__.py").write_text("VERSION = 1\n")    # same bytes, new mtime
    os.utime(pkg / "__init__.py", (0, 0))

    assert prov.snapshot(build_info=loaded)["source_changed_since_start"] is False


def test_the_fingerprint_notices_a_new_or_moved_file(tmp_path):
    repo, pkg = _repo(tmp_path)
    before, _ = prov.fingerprint_source(str(pkg))

    (pkg / "extra.py").write_text("x = 1\n")
    after, n = prov.fingerprint_source(str(pkg))
    assert after != before and n == 2

    os.rename(pkg / "extra.py", pkg / "renamed.py")
    moved, _ = prov.fingerprint_source(str(pkg))
    assert moved != after, "a rename with identical content must still register"


# ── 3. grading against an expected ref ────────────────────────────────


def test_a_build_containing_the_expected_ref_is_current(tmp_path):
    repo, pkg = _repo(tmp_path)
    expected = _head(repo)
    (pkg / "__init__.py").write_text("VERSION = 2\n")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "later", cwd=repo)

    snap = prov.snapshot(build_info=prov.capture(str(pkg)))
    assert prov.compare(expected, snap=snap)["status"] == prov.CURRENT


def test_a_build_predating_the_expected_ref_is_stale(tmp_path):
    """★The #247 shape: the fix is merged, the process is older."""
    repo, pkg = _repo(tmp_path)
    loaded = prov.capture(str(pkg))                       # process starts on old code
    (pkg / "fix.py").write_text("fixed = True\n")
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "the merged fix", cwd=repo)
    merged = _head(repo)

    result = prov.compare(merged, snap=prov.snapshot(build_info=loaded))

    assert result["status"] == prov.STALE
    assert "predates" in result["reason"] or "no longer on disk" in result["reason"]


def test_changed_source_overrides_a_current_sha(tmp_path):
    """⛔A matching SHA is not enough. If the loaded files are gone from disk,
    the process is running code nobody can read any more — reporting that as
    `current` would be the same false assurance #247 was given."""
    repo, pkg = _repo(tmp_path)
    loaded = prov.capture(str(pkg))
    expected = loaded["commit"]
    (pkg / "__init__.py").write_text("VERSION = 3\n")     # loaded bytes now gone

    result = prov.compare(expected, snap=prov.snapshot(build_info=loaded))

    assert result["status"] == prov.STALE
    assert "no longer on disk" in result["reason"]


@pytest.mark.parametrize("expected,expect_reason", [
    ("", "no expected ref"),
    ("refs/heads/does-not-exist", "not resolvable"),
])
def test_unanswerable_comparisons_report_unknown_not_current(tmp_path, expected, expect_reason):
    """⛔`unknown` is never collapsed into `current` — #248 requires a build we
    cannot grade to fail validation, not to pass quietly."""
    repo, pkg = _repo(tmp_path)
    snap = prov.snapshot(build_info=prov.capture(str(pkg)))

    result = prov.compare(expected, snap=snap)

    assert result["status"] == prov.UNKNOWN
    assert expect_reason in result["reason"]


def test_a_non_git_source_root_is_unknown_not_a_crash(tmp_path):
    pkg = tmp_path / "plain" / "src" / "agent_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")

    b = prov.capture(str(pkg))
    assert b["commit"] == ""
    assert prov.compare("anything", snap=prov.snapshot(build_info=b))["status"] == prov.UNKNOWN


def test_provenance_never_raises_when_git_is_unusable(tmp_path, monkeypatch):
    """A health endpoint must answer even on a host with no git."""
    repo, pkg = _repo(tmp_path)      # build the fixture BEFORE breaking git:
                                     # prov.subprocess is the shared module, so
                                     # patching it patches this file's helpers too.

    def boom(*a, **k):
        raise FileNotFoundError("git: not found")

    monkeypatch.setattr(prov.subprocess, "run", boom)

    b = prov.capture(str(pkg))
    assert b["commit"] == "" and b["code_fingerprint"]
    assert prov.compare("HEAD", snap=prov.snapshot(build_info=b))["status"] == prov.UNKNOWN


# ── 4. read-only by contract ──────────────────────────────────────────


def test_provenance_only_ever_runs_read_only_git(tmp_path, monkeypatch):
    """⛔#248's safety line: this must never pull, reset, checkout or restart.

    Asserted on the actual argv rather than by reading the code, so a future
    edit that adds a `git pull` here fails the suite instead of shipping.
    """
    repo, pkg = _repo(tmp_path)      # fixture first — its own `git init` is not
                                     # part of what provenance runs.
    seen = []
    real = subprocess.run

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(prov.subprocess, "run", spy)
    snap = prov.snapshot(build_info=prov.capture(str(pkg)))
    prov.compare(snap["commit"], snap=snap)

    read_only = {"rev-parse", "status", "merge-base", "cat-file", "show-ref", "log"}
    for argv in seen:
        assert argv[0] == "git", argv
        verb = argv[3] if argv[1] == "-C" else argv[1]
        assert verb in read_only, f"provenance ran a non-read-only git verb: {argv}"


# ── 5. the operational surface ────────────────────────────────────────


def _client(tmp_db, project="agent_crew"):
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    return TestClient(create_app(db_path=tmp_db, pane_map={}, port=8105,
                                 project=project, watchdog_disabled=True,
                                 anomaly_disabled=True))


def test_health_carries_the_build(tmp_db):
    """The thing that polls "is it up?" must also answer "up on what?"."""
    with _client(tmp_db) as c:
        body = c.get("/health").json()

    assert body["status"] == "ok"
    b = body["build"]
    assert b["commit"] and b["code_fingerprint"]
    assert b["pid"] and b["started_at"]
    assert b["source_root"].endswith("agent_crew")
    assert b["checkout_moved_since_start"] is False


def test_provenance_endpoint_grades_against_an_expected_ref(tmp_db):
    with _client(tmp_db) as c:
        snap = c.get("/provenance").json()
        graded = c.get("/provenance", params={"expect": snap["commit"]}).json()
        stale = c.get("/provenance", params={"expect": "refs/heads/nope"}).json()

    assert snap["project"] == "agent_crew" and snap["port"] == 8105
    assert graded["expected"]["status"] == prov.CURRENT
    assert stale["expected"]["status"] == prov.UNKNOWN


def test_startup_records_the_build_in_the_durable_event_stream(tmp_path):
    """#248 AC4: a before/after cohort must be cuttable on the PROCESS
    boundary. Merge time is not that boundary — #247 is the proof."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    db = str(tmp_path / "t.db")
    with TestClient(create_app(db_path=db, pane_map={}, port=8105,
                               project="alpha_engine", watchdog_disabled=True,
                               anomaly_disabled=True)):
        pass

    events = [json.loads(l) for l in open(tmp_path / "context_events.jsonl")]
    rec = [e for e in events if e["event_type"] == "build_provenance"]
    assert len(rec) == 1
    assert rec[0]["project"] == "alpha_engine"
    assert rec[0]["commit"] and rec[0]["code_fingerprint"]
    assert rec[0]["started_at"] and rec[0]["pid"]


def test_a_broken_provenance_read_does_not_stop_the_server(tmp_path, monkeypatch):
    """Observability may never be the thing that takes a dispatcher down."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    monkeypatch.setattr("agent_crew.server._prov.snapshot",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    db = str(tmp_path / "t.db")
    with TestClient(create_app(db_path=db, pane_map={}, port=8105,
                               project="p", watchdog_disabled=True,
                               anomaly_disabled=True)) as c:
        assert c.get("/tasks").status_code == 200


# ── 6. the CLI gate ───────────────────────────────────────────────────


def test_a_server_too_old_to_answer_is_stale_not_unreachable(monkeypatch, tmp_path):
    """★The first thing this tool meets in production.

    A pre-#248 dispatcher answers 404 on /provenance. Reporting that as
    "unreachable" would file the exact condition #248 exists to surface under
    "probably just down" — so it is a staleness verdict with its own label, and
    it fails the gate.
    """
    import urllib.error

    from click.testing import CliRunner

    from agent_crew import cli as crew_cli

    (tmp_path / "alpha_engine").mkdir()
    (tmp_path / "alpha_engine" / "state.json").write_text(json.dumps({"port": 8101}))

    def not_found(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    res = CliRunner().invoke(crew_cli.crew, ["provenance", "--base", str(tmp_path)])

    assert "STALE" in res.output and "pre-#248" in res.output
    assert res.exit_code == 1, "a stale runtime must fail the gate"


def test_a_down_server_is_reported_but_is_not_called_stale(monkeypatch, tmp_path):
    """⛔"I could not ask" is not "it is old" — the same distinction `compare`
    keeps between unknown and stale."""
    from click.testing import CliRunner

    from agent_crew import cli as crew_cli

    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "state.json").write_text(json.dumps({"port": 8199}))

    def refused(*a, **k):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr("urllib.request.urlopen", refused)
    res = CliRunner().invoke(crew_cli.crew, ["provenance", "--base", str(tmp_path)])

    assert "UNREACHABLE" in res.output
    assert "STALE" not in res.output


def test_the_gate_passes_only_when_every_server_is_current(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from agent_crew import cli as crew_cli

    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "state.json").write_text(json.dumps({"port": 8101}))

    bodies = iter([
        {"commit": "aaa", "commit_short": "aaa", "ref": "main", "dirty": False,
         "uptime_s": 10, "checkout_moved_since_start": False,
         "source_changed_since_start": False,
         "expected": {"status": "current", "reason": "ok"}},
        {"commit": "bbb", "commit_short": "bbb", "ref": "main", "dirty": False,
         "uptime_s": 10, "checkout_moved_since_start": False,
         "source_changed_since_start": False,
         "expected": {"status": "stale", "reason": "loaded build predates 98d869d"}},
    ])

    class _R:
        def __init__(self, body): self._b = json.dumps(body).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _R(next(bodies)))
    res = CliRunner().invoke(crew_cli.crew,
                             ["provenance", "--base", str(tmp_path), "--expect", "98d869d"])

    assert "STALE" in res.output
    assert "predates 98d869d" in res.output
    assert res.exit_code == 1


def test_identity_falls_back_to_the_state_directory(tmp_path):
    """⛔The module-level `app` every live server is launched from never passes
    `project`, so an identity that depends on it would report "" for all four
    dispatchers — and a provenance gate that cannot name the server it is
    grading is not a gate."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    proj = tmp_path / "alpha_engine"
    proj.mkdir()
    db = str(proj / "tasks.db")
    with TestClient(create_app(db_path=db, pane_map={}, port=8101,
                               watchdog_disabled=True, anomaly_disabled=True)) as c:
        health = c.get("/health").json()
        snap = c.get("/provenance").json()

    assert health["project"] == "alpha_engine"          # not passed, derived
    assert health["identity"]["db_path"] == db
    assert snap["identity"]["project"] == "alpha_engine"
    rec = [json.loads(l) for l in open(proj / "context_events.jsonl")]
    assert [e for e in rec if e["event_type"] == "build_provenance"][0]["project"] == "alpha_engine"
