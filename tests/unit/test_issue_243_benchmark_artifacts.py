"""#243 — the committed benchmark artifacts must agree with each other.

PR #241 shipped `context_pack_benchmark.json` saying lexical p50/p95 =
37.5/68.7 and `context_pack_benchmark.md` saying 32.1/39.3, for the same
revision. Every deterministic metric matched; only latency diverged —
because latency is the one nondeterministic metric and I generated the two
artifacts from separate runs, hand-writing the Markdown table from an
earlier run's terminal output.

That is a measurement-integrity bug, not a cosmetic one: quota-core#62 and
quota-ops#21 are meant to consume these as production baselines, and two
representations of one revision disagreeing means a consumer's answer
depends on which file it happened to read.

These tests make the divergence impossible to commit again:
  * the Markdown is a pure function of the canonical JSON object;
  * the two committed files are checked field-by-field against each other.
"""

import json
import os
import re
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
JSON_PATH = os.path.join(REPO_ROOT, "docs", "context_pack_benchmark.json")
MD_PATH = os.path.join(REPO_ROOT, "docs", "context_pack_benchmark.md")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

benchmark_context_pack = pytest.importorskip("benchmark_context_pack")


def _parse_markdown_table(text: str) -> dict:
    """Pull `{mode: {column: value}}` back out of the rendered table."""
    rows, header = {}, None
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue                      # separator row
        mode, values = cells[0], cells[1:]
        rows[mode] = dict(zip(header[1:], values))
    return rows


@pytest.fixture
def report():
    """A real report object from the offline sample — no network, one run."""
    m = benchmark_context_pack
    issues = m.OFFLINE_SAMPLE
    return m.build_report(issues, ["current", m.MODE_LEXICAL],
                          sample_label="offline sample")


# ── 1. the Markdown is derived, never hand-written ────────────────────


def test_markdown_is_a_pure_function_of_the_report(report):
    """★The root cause: the table used to be typed by hand from another run."""
    m = benchmark_context_pack
    assert m.render_markdown(report) == m.render_markdown(report)


def test_rendered_markdown_matches_its_own_report(report):
    """Every column in the rendered table equals the object it came from."""
    m = benchmark_context_pack
    table = _parse_markdown_table(m.render_markdown(report))

    for mode in report["_modes"]:
        for col in m.REPORT_COLUMNS:
            assert table[mode][col] == str(report[mode][col]), (mode, col)


def test_latency_columns_are_included(report):
    """⛔The diverging fields specifically — they must be in both forms."""
    m = benchmark_context_pack
    table = _parse_markdown_table(m.render_markdown(report))

    for mode in report["_modes"]:
        assert table[mode]["p50_ms"] == str(report[mode]["p50_ms"])
        assert table[mode]["p95_ms"] == str(report[mode]["p95_ms"])


def test_report_carries_run_identity(report):
    """Latency variability stays visible: a reader can tell runs apart."""
    assert re.fullmatch(r"[0-9a-f]{12}", report["_run_id"])
    assert report["_generated_at"].endswith("+00:00")
    assert "canonical" in report["_canonical"]
    assert "only within one run_id" in report["_latency_note"]


def test_write_report_emits_both_from_one_object(tmp_path, report):
    """★Both artifacts, one run, one object — the actual fix."""
    m = benchmark_context_pack
    j, md = m.write_report(report, str(tmp_path))

    loaded = json.load(open(j))
    table = _parse_markdown_table(open(md).read())

    assert loaded["_run_id"] == report["_run_id"]
    for mode in report["_modes"]:
        for col in m.REPORT_COLUMNS:
            assert table[mode][col] == str(loaded[mode][col]), (mode, col)


# ── 2. the COMMITTED artifacts agree (the CI guard #243 asks for) ─────


@pytest.mark.skipif(not os.path.exists(JSON_PATH) or not os.path.exists(MD_PATH),
                    reason="benchmark artifacts not present on this branch")
def test_committed_artifacts_agree_on_every_shared_metric():
    """★★The guard. Would have failed on PR #241 as committed.

    Compares the two files on disk field-by-field. If someone regenerates one
    and not the other — or hand-edits the table — this fails.
    """
    m = benchmark_context_pack
    data = json.load(open(JSON_PATH))
    table = _parse_markdown_table(open(MD_PATH).read())

    modes = data.get("_modes") or [k for k in data if not k.startswith("_")]
    assert modes, "the JSON declares no modes"

    mismatches = []
    for mode in modes:
        assert mode in table, f"mode {mode!r} missing from the Markdown table"
        for col in m.REPORT_COLUMNS:
            want, got = str(data[mode][col]), table[mode][col]
            if want != got:
                mismatches.append(f"{mode}.{col}: json={want} md={got}")
    assert not mismatches, (
        "committed benchmark artifacts disagree — regenerate BOTH with "
        "`--write-report docs`:\n  " + "\n  ".join(mismatches))


@pytest.mark.skipif(not os.path.exists(JSON_PATH) or not os.path.exists(MD_PATH),
                    reason="benchmark artifacts not present on this branch")
def test_committed_artifacts_share_a_run_id():
    """⛔Same revision means same run. Different run_ids mean two runs were
    committed together, which is how #243 happened in the first place."""
    data = json.load(open(JSON_PATH))
    md = open(MD_PATH).read()

    assert data.get("_run_id"), "JSON has no run_id"
    assert data["_run_id"] in md, (
        "the Markdown does not carry the JSON's run_id — the two artifacts "
        "were not produced by the same run")


@pytest.mark.skipif(not os.path.exists(MD_PATH), reason="no markdown artifact")
def test_markdown_names_the_canonical_artifact():
    """#243 acceptance: the docs point at one canonical result."""
    md = open(MD_PATH).read()
    assert "context_pack_benchmark.json is canonical" in md
