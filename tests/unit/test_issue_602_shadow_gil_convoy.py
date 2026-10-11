"""Shadow SQLite reads remain equivalent and bounded under Python CPU contention."""

import json
import sqlite3

import agent_crew.memory_runtime as runtime
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage


def _insert(db, layer, key, value, scope, created, version=1):
    db.execute(
        "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
        (layer, key, json.dumps(value), json.dumps(scope), version, created),
    )


def _four_query_reference(storage, scope, limit, predecessor_task_ids=(), pr_number=None):
    """Row-by-row reference with the shadow fleet-ancestor rule."""
    fleet_ancestor = ("(COALESCE(json_extract(scope,'$.project'),'')='' "
                      "AND COALESCE(json_extract(scope,'$.fleet'),'')<>'')")
    base = ("layer IN ('decision','episodic') "
            "AND (COALESCE(json_extract(scope,'$.fleet'),'')=? OR "
            + fleet_ancestor + ") "
            "AND (json_extract(scope,'$.task_id') IS NULL "
            "OR json_extract(scope,'$.task_id')='' "
            "OR json_extract(scope,'$.task_id')=?) "
            "AND json_extract(value,'$.invalidated_at') IS NULL "
            "AND json_extract(value,'$.superseded_at') IS NULL "
            "AND COALESCE(json_extract(value,'$.superseded'),0)=0")
    with sqlite3.connect(storage.path) as db:
        dropped = db.execute(
            "SELECT count(*) FROM adr001_memory WHERE " + base +
            " AND COALESCE(json_extract(scope,'$.project'),'')=''"
            " AND COALESCE(json_extract(scope,'$.fleet'),'')=''",
            (scope.fleet, scope.task_id)).fetchone()[0]
        scoped_rows = db.execute(
            "SELECT layer,key,value,scope,version FROM adr001_memory WHERE " + base +
            " AND (COALESCE(json_extract(scope,'$.project'),'')=? OR "
            + fleet_ancestor + ") "
            "ORDER BY created DESC LIMIT ?",
            (scope.fleet, scope.task_id, scope.project, limit)).fetchall()
        predecessor_keys = [f"task:{task_id}:decision" for task_id in predecessor_task_ids]
        lineage_rows = db.execute(
            "SELECT layer,key,value,scope,version FROM adr001_memory "
            "WHERE layer='decision' AND key IN (" + ",".join("?" for _ in predecessor_keys)
            + ") AND COALESCE(json_extract(scope,'$.project'),'')=?",
            (*predecessor_keys, scope.project),
        ).fetchall() if predecessor_keys else []
        same_pr_rows = db.execute(
            "SELECT layer,key,value,scope,version FROM adr001_memory "
            "WHERE layer='decision' AND COALESCE(json_extract(scope,'$.project'),'')=? "
            "AND json_extract(value,'$.pr_number')=? "
            "AND json_extract(value,'$.task_id')<>? "
            "ORDER BY created DESC LIMIT 50",
            (scope.project, pr_number, scope.task_id),
        ).fetchall() if pr_number else []

    def decode(row):
        return MemoryRecord(row[0], row[1], json.loads(row[2]),
                            runtime._scope_from_json(row[3]), row[4])

    scoped = []
    for row in scoped_rows:
        record = decode(row)
        if runtime._scope_applies(record.scope, scope, strict=False):
            scoped.append(record)
    lineage = {record.key: record for record in map(decode, lineage_rows)}
    pinned = [lineage[key] for key in predecessor_keys if key in lineage
              and lineage[key].scope.fleet in ("", scope.fleet)
              and not lineage[key].value.get("invalidated_at")
              and not lineage[key].value.get("superseded_at")
              and not lineage[key].value.get("superseded")]
    same_pr = [record for record in map(decode, same_pr_rows)
               if record.scope.fleet in ("", scope.fleet)
               and not record.value.get("invalidated_at")
               and not record.value.get("superseded_at")
               and not record.value.get("superseded")]
    pinned_keys = {record.key for record in pinned}
    selected = pinned + [record for record in same_pr if record.key not in pinned_keys]
    selected_keys = {record.key for record in selected}
    return (selected + [record for record in scoped if record.key not in selected_keys])[:limit], dropped


def test_aggregated_shadow_rows_preserve_lineage_pr_scope_and_order(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    with sqlite3.connect(storage.path) as db:
        _insert(db, "decision", "task:older:decision",
                {"task_id": "older", "pr_number": 602},
                {"project": "agent_crew", "task_id": "older"}, 1)
        _insert(db, "decision", "task:newer:decision",
                {"task_id": "newer", "pr_number": 602},
                {"project": "agent_crew", "task_id": "newer"}, 2)
        _insert(db, "decision", "task:same-pr:decision",
                {"task_id": "same-pr", "pr_number": 602},
                {"project": "agent_crew", "task_id": "same-pr"}, 4)
        _insert(db, "episodic", "recent-scoped", {"text": "recent"},
                {"project": "agent_crew"}, 5)
        _insert(db, "episodic", "older-scoped", {"text": "older"},
                {"project": "agent_crew"}, 3)
        _insert(db, "decision", "foreign-pr", {"task_id": "foreign", "pr_number": 602},
                {"project": "another"}, 10)
        _insert(db, "episodic", "projectless", {}, {}, 9)
        _insert(db, "episodic", "retired", {"superseded_at": 1},
                {"project": "agent_crew"}, 8)
        db.commit()

    rows, dropped = storage.retrieve_shadow(
        MemoryScope(project="agent_crew", task_id="current"),
        {"decision", "episodic"}, 10, predecessor_task_ids=("older", "newer"),
        pr_number=602,
    )
    assert dropped == 1
    assert rows == [
        MemoryRecord("decision", "task:older:decision",
                     {"task_id": "older", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="older")),
        MemoryRecord("decision", "task:newer:decision",
                     {"task_id": "newer", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="newer")),
        MemoryRecord("decision", "task:same-pr:decision",
                     {"task_id": "same-pr", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="same-pr")),
        MemoryRecord("episodic", "recent-scoped", {"text": "recent"},
                     MemoryScope(project="agent_crew")),
        MemoryRecord("episodic", "older-scoped", {"text": "older"},
                     MemoryScope(project="agent_crew")),
    ]


def test_aggregated_rows_match_four_query_path_with_ties_limits_and_fleet(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "equivalence.db"))
    with sqlite3.connect(storage.path) as db:
        _insert(db, "decision", "task:old:decision",
                {"task_id": "old", "pr_number": 633},
                {"project": "agent_crew", "task_id": "old"}, 1, version=7)
        _insert(db, "decision", "task:new:decision",
                {"task_id": "new", "pr_number": 633},
                {"project": "agent_crew", "fleet": "named", "task_id": "new"}, 2,
                version=3)
        for number in range(4):
            _insert(db, "decision", f"task:peer-{number}:decision",
                    {"task_id": f"peer-{number}", "pr_number": 633},
                    {"project": "agent_crew", "fleet": "named"}, 20 + number // 2,
                    version=number + 2)
        for number in range(8):
            _insert(db, "episodic", f"scoped-{number}", {"text": str(number)},
                    {"project": "agent_crew", "fleet": "named"},
                    30 + number // 2, version=number + 1)
        _insert(db, "decision", "other-fleet", {"task_id": "other", "pr_number": 633},
                {"project": "agent_crew", "fleet": "other"}, 40)
        _insert(db, "decision", "other-project", {"task_id": "foreign", "pr_number": 633},
                {"project": "elsewhere", "fleet": "named"}, 41)
        _insert(db, "episodic", "projectless", {}, {"fleet": "named"}, 42)
        _insert(db, "episodic", "retired", {"superseded_at": 1},
                {"project": "agent_crew", "fleet": "named"}, 43)
        db.commit()

    scope = MemoryScope(fleet="named", project="agent_crew", task_id="current")
    for predecessors, pr_number, limit in (
            (("old", "new"), 633, 5), ((), None, 3)):
        expected = _four_query_reference(storage, scope, limit, predecessors, pr_number)
        actual = storage.retrieve_shadow(
            scope, {"decision", "episodic"}, limit,
            predecessor_task_ids=predecessors, pr_number=pr_number)
        assert actual == expected
        assert expected[1] == 0
        assert len(actual[0]) == limit
        if not predecessors:
            assert "projectless" in {record.key for record in actual[0]}
    assert any(record.version > 1 for record in
               storage.retrieve_shadow(scope, {"decision", "episodic"}, 5,
                                       predecessor_task_ids=("old", "new"),
                                       pr_number=633)[0])


def test_shadow_query_fetch_count_is_constant_with_3000_rows(tmp_path, monkeypatch):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    value_json = json.dumps({"task_id": "old", "pr_number": 602})
    with sqlite3.connect(storage.path) as db:
        db.executemany(
            "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
            (("decision", f"task:old-{i}:decision", value_json,
              json.dumps({"project": "agent_crew" if i < 750 else f"other-{i % 3}"}), 1, i)
             for i in range(3000)),
        )
        db.commit()

    calls = {"execute": 0, "fetchone": 0, "fetchall": 0}
    real_connect = sqlite3.connect

    class CountedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            calls["fetchone"] += 1
            return self.cursor.fetchone()

        def fetchall(self):
            calls["fetchall"] += 1
            return self.cursor.fetchall()

    class CountedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args, **kwargs):
            calls["execute"] += 1
            return CountedCursor(self.connection.execute(*args, **kwargs))

        def close(self):
            self.connection.close()

    monkeypatch.setattr(runtime.sqlite3, "connect",
                        lambda *a, **k: CountedConnection(real_connect(*a, **k)))
    timing = {}
    records, dropped = storage.retrieve_shadow(
        MemoryScope(project="agent_crew", task_id="current"),
        {"decision"}, 10, predecessor_task_ids=("old-1", "old-2"),
        pr_number=602, timing=timing)
    assert len(records) == 10 and dropped == 0
    assert timing["query_ms"] >= 0
    assert calls == {"execute": 1, "fetchone": 1, "fetchall": 0}
