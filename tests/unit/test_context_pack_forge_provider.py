"""Forge retrieval stays optional and cannot block context pack construction."""

import json
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
    assert request.full_url == "http://127.0.0.1:9002/get_context"
    assert timeout == 5.0
    assert json.loads(request.data)["situation"] == {
        "project": "agent_crew", "task_type": "implement", "fix_round": True,
    }


@pytest.mark.parametrize("error", [URLError("refused"), TimeoutError("timeout")])
def test_failure_degrades_but_builds(monkeypatch, error):
    monkeypatch.setenv("AGENT_CREW_FORGE_PROVIDER", "1")
    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(error))
    pack = _build()
    assert pack.degraded
    assert pack.telemetry()["forge_items"] == 0
    assert any("forge_crew" in e for e in pack.provider_errors)


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
