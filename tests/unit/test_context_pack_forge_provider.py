"""Forge retrieval stays optional and cannot block context pack construction."""

import json
import threading
import time
from pathlib import Path
from urllib.error import URLError

import pytest

from agent_crew import context_pack as cp


FIXTURE = Path(__file__).parents[1] / "fixtures/context_pack/forge-get-context.json"


def _build():
    return cp.build_pack_for_task(
        {"issue_title": "dispatch context", "repo": "org/agent_crew"},
        task_id="fix-1", task_type="implement", role="implementer",
    )


def test_off_does_not_construct_or_call_forge(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_FORGE_PROVIDER", raising=False)
    monkeypatch.setattr(cp.ForgeProvider, "__init__", lambda *a, **kw: pytest.fail("constructed"))
    pack = _build()
    assert pack.telemetry()["forge_items"] == 0
    assert pack.mode == cp.MODE_LEXICAL


def test_real_shape_maps_artifacts_and_request(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, *_):
            return FIXTURE.read_bytes()

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(cp.urllib.request, "urlopen", fake_urlopen)
    pack = _build()
    items = {a.artifact_id: a for a in pack.items}
    assert pack.telemetry()["forge_items"] == 2
    assert pack.mode == cp.MODE_HYBRID
    assert items["forge:crew-adr-17"].artifact_type == cp.TYPE_ADR
    assert items["forge:crew-spec-9"].artifact_type == cp.TYPE_SPEC
    assert items["forge:crew-adr-17"].score > items["forge:crew-spec-9"].score
    assert "high" in items["forge:crew-adr-17"].provenance
    assert "hybrid" in items["forge:crew-adr-17"].provenance
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8769/get_context"
    assert timeout == 0.3
    body = json.loads(request.data)
    assert body["situation"] == {
        "project": "agent_crew", "task_type": "implement", "fix_round": True,
    }
    assert (body["repo"], body["project"], body["role"], body["byte_budget"]) == (
        "org/agent_crew", "agent_crew", "implementer", 8000)


def test_live_shape_maps_tail_and_telemetry(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    tail_text = "x" * 900
    body = {"items": [{"source_path": "src/worker.py", "repo": "org/agent_crew",
                       "commit_sha": "a" * 40, "content_sha": "b" * 64,
                       "bytes": 900, "score": 0.7, "text": tail_text}],
            "mode": "hybrid", "model_id": "small", "tail_bytes": 900}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(body).encode()

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: Response())
    pack = _build()
    forge = [a for a in pack.items if a.artifact_id.startswith("forge:")]
    assert len(forge) == 1
    assert forge[0].uri == "src/worker.py"
    assert forge[0].revision == "a" * 40
    assert forge[0].excerpt == tail_text
    assert pack.telemetry()["tail_bytes"] == 900


def test_tail_bytes_counts_only_delivered_forge_text(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:src/duplicate.py", "src/duplicate.py", cp.TYPE_CODE)
    ])
    body = {"items": [
        {"source_path": path, "repo": "org/agent_crew", "commit_sha": "a" * 40,
         "content_sha": sha * 64, "bytes": len(text.encode()), "score": score,
         "text": text}
        for path, sha, text, score in (("src/duplicate.py", "a", "duplicate", 0.7),
                                       ("src/delivered.py", "b", "café", 0.9),
                                       ("src/dropped.py", "c", "dropped", 0.1))
    ], "tail_bytes": 9999}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(body).encode()

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: Response())
    query = cp.RetrievalQuery(task_id="fix-1", role="implementer")
    providers = [cp.LexicalRepoProvider(), cp.ForgeProvider()]
    pack = cp.plan_pack(query, providers, mode=cp.MODE_HYBRID,
                        budget={"max_tokens": 1000, "max_items": 2})
    assert pack.telemetry()["forge_items"] == 1
    assert pack.telemetry()["tail_bytes"] == len("café".encode())

    dropped = cp.plan_pack(query, providers, mode=cp.MODE_HYBRID,
                           budget={"max_tokens": 1000, "max_items": 1})
    assert dropped.telemetry()["forge_items"] == 0
    assert dropped.telemetry()["tail_bytes"] == 0


def test_tail_bytes_without_server_count_uses_delivered_text(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    body = {"items": [{"source_path": "src/x.py", "repo": "org/agent_crew",
                       "commit_sha": "a" * 40, "content_sha": "b" * 64,
                       "bytes": 5, "score": 0.7, "text": "café"}]}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(body).encode()

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: Response())
    assert _build().telemetry()["tail_bytes"] == len("café".encode())


def test_url_and_timeout_use_environment(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_URL", "http://127.0.0.1:9123/")
    monkeypatch.setenv("AGENT_CREW_FORGE_TIMEOUT_MS", "125")
    provider = cp.ForgeProvider()
    assert provider._url == "http://127.0.0.1:9123"
    assert provider._timeout == 0.125


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "nan"])
def test_invalid_timeout_environment_falls_back_to_300ms(monkeypatch, raw):
    monkeypatch.setenv("AGENT_CREW_FORGE_TIMEOUT_MS", raw)
    assert cp.ForgeProvider()._timeout == 0.3


def test_default_timeout_omits_tail_and_counts_once(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    release = threading.Event()
    exited = threading.Event()

    def stalled(*a, **k):
        try:
            release.wait(1)
            return None
        finally:
            exited.set()

    monkeypatch.setattr(cp.urllib.request, "urlopen", stalled)
    started = time.monotonic()
    try:
        pack = _build()
    finally:
        release.set()
        exited.wait(1)
    assert time.monotonic() - started < 0.6
    assert pack.telemetry()["forge_timeout"] == 1
    assert "timeout" in pack.telemetry()["forge_error"]
    assert pack.telemetry()["tail_bytes"] == 0
    assert pack.telemetry()["forge_items"] == 0


@pytest.mark.parametrize("error", [URLError("refused"), TimeoutError("timeout")])
def test_failure_degrades_but_builds(monkeypatch, error):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:src/worker.py", "src/worker.py", cp.TYPE_CODE)
    ])
    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(error))
    pack = _build()
    assert pack.degraded
    assert pack.telemetry()["forge_items"] == 0
    assert pack.mode == cp.MODE_LEXICAL
    assert "repo:src/worker.py" in {item.artifact_id for item in pack.items}
    assert any("forge_crew" in e for e in pack.provider_errors)


@pytest.mark.parametrize("body", [
    b"not-json",
    b"[]",
    b"{}",
    b'{"context_items": {}}',
    b'{"context_items": [{"chunk_id": "missing-source"}]}',
    b'{"context_items": [{"chunk_id": "missing-score", "source_file": "src/x.py", "content": "x"}]}',
])
def test_malformed_forge_response_keeps_lexical_results(monkeypatch, body):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:src/worker.py", "src/worker.py", cp.TYPE_CODE)
    ])

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, *_):
            return body

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **kw: Response())
    pack = _build()
    assert pack.mode == cp.MODE_LEXICAL
    assert pack.degraded and pack.provider_errors
    assert pack.telemetry()["forge_error"]
    assert pack.telemetry()["forge_items"] == 0
    assert "repo:src/worker.py" in {item.artifact_id for item in pack.items}


@pytest.mark.parametrize("change", [
    {"commit_sha": None}, {"content_sha": None}, {"text": None},
    {"bytes": "bad"}, {"bytes": -1},
])
def test_incomplete_live_item_omits_tail_and_records_error(monkeypatch, change):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    item = {"source_path": "src/x.py", "repo": "org/agent_crew",
            "commit_sha": "a" * 40, "content_sha": "b" * 64,
            "bytes": 4, "score": 0.7, "text": "text"}
    item.update(change)
    body = {"items": [item], "tail_bytes": 4}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(body).encode()

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: Response())
    pack = _build()
    assert pack.telemetry()["forge_items"] == 0
    assert pack.telemetry()["tail_bytes"] == 0
    assert pack.telemetry()["forge_error"]


@pytest.mark.parametrize("tail_bytes", ["bad", -1, None])
def test_invalid_server_tail_bytes_omits_tail_and_records_error(monkeypatch, tail_bytes):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    body = {"items": [{"source_path": "src/x.py", "repo": "org/agent_crew",
                       "commit_sha": "a" * 40, "content_sha": "b" * 64,
                       "bytes": 4, "score": 0.7, "text": "text"}],
            "tail_bytes": tail_bytes}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return json.dumps(body).encode()

    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **k: Response())
    pack = _build()
    assert pack.telemetry()["forge_items"] == 0
    assert pack.telemetry()["tail_bytes"] == 0
    assert pack.telemetry()["forge_error"]


def test_stalled_forge_read_is_bounded_and_keeps_lexical_results(monkeypatch):
    release = threading.Event()
    exited = threading.Event()
    calls = []

    def stalled_urlopen(*args, **kwargs):
        calls.append(1)
        try:
            release.wait(1)
            raise TimeoutError("stalled response")
        finally:
            exited.set()

    monkeypatch.setattr(cp.urllib.request, "urlopen", stalled_urlopen)
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:src/worker.py", "src/worker.py", cp.TYPE_CODE)
    ])
    started = time.monotonic()
    try:
        pack = cp.plan_pack(
            cp.RetrievalQuery(task_id="fix-1", role="implementer"),
            [cp.LexicalRepoProvider(), cp.ForgeProvider(timeout_s=0.05)],
            mode=cp.MODE_HYBRID,
        )
        another = cp.ForgeProvider(timeout_s=0.05)
        assert another.retrieve(cp.RetrievalQuery(task_id="fix-2")) == []
        assert "still in progress" in another.last_error
    finally:
        release.set()
        exited.wait(1)
    assert time.monotonic() - started < 0.5
    assert pack.degraded and "timeout" in pack.degraded_reason
    assert "repo:src/worker.py" in {item.artifact_id for item in pack.items}
    assert calls == [1], "a stalled Forge call spawned another HTTP worker"


def test_lexical_path_wins_over_forge(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:docs/adr/0017-context.md", "docs/adr/0017-context.md", cp.TYPE_ADR)
    ])
    monkeypatch.setattr(cp.ForgeProvider, "retrieve", lambda self, query: [
        self._to_artifact(item) for item in json.loads(FIXTURE.read_text())["context_items"]
    ])
    pack = _build()
    assert pack.telemetry()["forge_items"] == 1
    assert "forge:crew-adr-17" not in {a.artifact_id for a in pack.items}
    assert "forge:crew-spec-9" in {a.artifact_id for a in pack.items}


def test_forge_adr_label_has_no_authority_rank(monkeypatch):
    lexical_adr = cp.Artifact(
        "repo:docs/adr/local.md", "docs/adr/local.md", cp.TYPE_ADR,
        score=0.1,
    )
    lexical_code = cp.Artifact(
        "repo:src/local.py", "src/local.py", cp.TYPE_CODE,
        score=0.9,
    )
    forge_adr = cp.ForgeProvider._to_artifact({
        "chunk_id": "remote-adr", "source_file": "docs/adr/remote.md",
        "content": "remote decision", "score": 1.0,
        "reliability_label": "ADR", "source_reliability": 1.0,
    })
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        lexical_adr, lexical_code,
    ])
    monkeypatch.setattr(cp.ForgeProvider, "retrieve", lambda self, query: [forge_adr])
    query = cp.RetrievalQuery(task_id="fix-1", role="implementer")
    providers = [cp.LexicalRepoProvider(), cp.ForgeProvider()]

    ordered = cp.plan_pack(query, providers, mode=cp.MODE_HYBRID,
                           budget={"max_tokens": 1000, "max_items": 3}).items
    assert [item.artifact_id for item in ordered] == [
        lexical_adr.artifact_id, lexical_code.artifact_id, forge_adr.artifact_id,
    ]
    selected = cp.plan_pack(query, providers, mode=cp.MODE_HYBRID,
                            budget={"max_tokens": 1000, "max_items": 1}).items
    assert [item.artifact_id for item in selected] == [lexical_adr.artifact_id]


def test_same_basename_different_paths_survive_but_exact_duplicates_do_not(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve", lambda self, query: [
        cp.Artifact("repo:docs/README.md", "docs/README.md", cp.TYPE_SPEC)
    ])
    monkeypatch.setattr(cp.ForgeProvider, "retrieve", lambda self, query: [
        cp.Artifact("forge:src-readme", "src/README.md", cp.TYPE_SPEC),
        cp.Artifact("forge:docs-readme", "docs/README.md", cp.TYPE_SPEC),
        cp.Artifact("forge:src-readme", "src/README.md", cp.TYPE_SPEC),
    ])

    ids = [a.artifact_id for a in _build().items]
    assert ids.count("repo:docs/README.md") == 1
    assert ids.count("forge:src-readme") == 1
    assert "forge:docs-readme" not in ids


@pytest.mark.parametrize("forge_path,max_items", [
    ("docs/adr/local.md", 2),  # the lexical copy wins deduplication
    ("docs/adr/remote.md", 1),  # the budget excludes Forge
])
def test_hybrid_mode_requires_selected_forge_item(monkeypatch, forge_path, max_items):
    lexical = cp.Artifact("repo:local", "docs/adr/local.md", cp.TYPE_ADR)
    forge = cp.ForgeProvider._to_artifact({
        "chunk_id": "remote", "source_file": forge_path,
        "content": "remote decision", "score": 1.0,
    })
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve",
                        lambda self, query: [lexical])
    monkeypatch.setattr(cp.ForgeProvider, "retrieve", lambda self, query: [forge])
    pack = cp.plan_pack(
        cp.RetrievalQuery(task_id="fix-1", role="implementer"),
        [cp.LexicalRepoProvider(), cp.ForgeProvider()],
        budget={"max_tokens": 1000, "max_items": max_items}, mode=cp.MODE_HYBRID,
    )
    assert [item.artifact_id for item in pack.items] == [lexical.artifact_id]
    assert pack.mode == pack.telemetry()["mode"] == cp.MODE_LEXICAL
    assert pack.telemetry()["forge_items"] == 0


def test_hybrid_mode_counts_selected_forge_items(monkeypatch):
    lexical = cp.Artifact("repo:local", "docs/adr/local.md", cp.TYPE_ADR)
    forge_items = [cp.ForgeProvider._to_artifact({
        "chunk_id": f"remote-{index}", "source_file": f"docs/adr/remote-{index}.md",
        "content": "remote decision", "score": 1.0,
    }) for index in range(2)]
    monkeypatch.setattr(cp.LexicalRepoProvider, "retrieve",
                        lambda self, query: [lexical])
    monkeypatch.setattr(cp.ForgeProvider, "retrieve",
                        lambda self, query: forge_items)
    pack = cp.plan_pack(
        cp.RetrievalQuery(task_id="fix-1", role="implementer"),
        [cp.LexicalRepoProvider(), cp.ForgeProvider()],
        budget={"max_tokens": 1000, "max_items": 2}, mode=cp.MODE_HYBRID,
    )
    assert [item.artifact_id for item in pack.items] == [
        lexical.artifact_id, forge_items[0].artifact_id,
    ]
    assert pack.mode == pack.telemetry()["mode"] == cp.MODE_HYBRID
    assert pack.telemetry()["forge_items"] == 1
