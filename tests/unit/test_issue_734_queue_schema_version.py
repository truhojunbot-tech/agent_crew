"""#734: a queue DDL edit requires an explicit protocol version bump."""

import ast
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import QUEUE_SCHEMA_VERSION, TaskQueue
from agent_crew.server import create_app


# Update this map only when bumping QUEUE_SCHEMA_VERSION. Previous entries stay
# immutable so changing DDL under an existing version fails this test.
SCHEMA_SIGNATURES = {
    1: "5651630a8037b6c1af8b8a21af380f0501048a54b9e6df416047d26526c5b36f",
    2: "61b97344300aeedd35eeed29542d6e3d2ada4f414a50722bcd36067ac13f2bba",
    3: "d9dfc5889ab0b27f4fbdaf9ff0fac518eabb6e70eb40b3d21439d2b612c4ee60",
    4: "a644cdd96961ba0b7f50eea300d43b62619546f3a2da77beea88472dd7c724b2",
    5: "71bc777a9040d4176304cf26f7989bcea306e54c4c65cfd26276392e0015afad",
    6: "33b6087ca6482fd5906ddf251040820346e458edb80d66eaca323be2b962263f",
    7: "1804c5c60296bffde40d28b7358f4b7f6cc3d5db44832641ae826911ee104c12",
    8: "d7dc3d5928a0bae5122f50e9d8c4273d597e0874bf57b7bc622402a15475859f",
}
_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "agent_crew"
_DDL_SOURCES = {
    "queue.py": _SOURCE_ROOT / "queue.py",
    "cea/store.py": _SOURCE_ROOT / "cea" / "store.py",
}
_DDL_START = re.compile(r"^\s*(?:CREATE|ALTER|DROP)\s+", re.IGNORECASE)


def _schema_signature(sources: dict[str, str]) -> str:
    """Fingerprint DDL from both writers used by TaskQueue initialization."""
    statements = []
    for name, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and (
                            target.id.startswith("_DDL")
                            or target.id in {"_EXEC_STATE_COLUMN_TYPES", "_COLUMN_TYPES"}):
                        statements.append((
                            name, target.id, ast.dump(node.value, include_attributes=False)))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _DDL_START.match(node.value):
                    statements.append((name, "inline", node.value.strip()))
            elif isinstance(node, ast.JoinedStr):
                fragment = ast.get_source_segment(source, node) or ""
                if re.search(r"\b(?:CREATE|ALTER|DROP)\s+", fragment, re.IGNORECASE):
                    statements.append((name, "inline", ast.dump(node, include_attributes=False)))
    payload = repr(sorted(statements)).encode()
    return hashlib.sha256(payload).hexdigest()


def _ddl_sources() -> dict[str, str]:
    return {name: path.read_text() for name, path in _DDL_SOURCES.items()}


def test_health_exposes_queue_schema_version(tmp_path):
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="agent_crew",
                     push_fn=lambda *args, **kwargs: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["schema_version"] == QUEUE_SCHEMA_VERSION


def test_queue_ddl_requires_version_bump(tmp_path):
    sources = _ddl_sources()
    db = str(tmp_path / "tasks.db")
    TaskQueue(db)
    assert _schema_signature(sources) == SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION]


def test_ddl_change_without_bump_fails_guard(tmp_path):
    sources = _ddl_sources()
    db = str(tmp_path / "tasks.db")
    TaskQueue(db)
    changed = sources["queue.py"].replace(
        "CREATE TABLE IF NOT EXISTS gates (",
        "CREATE TABLE IF NOT EXISTS gates (\n    new_field TEXT,", 1)
    assert changed != sources["queue.py"]
    assert (_schema_signature({**sources, "queue.py": changed})
            != SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION])


def test_cea_store_ddl_change_without_bump_fails_guard(tmp_path):
    TaskQueue(str(tmp_path / "tasks.db"))
    sources = _ddl_sources()
    changed = sources["cea/store.py"].replace(
        "ALTER TABLE tasks ADD COLUMN receipt_id TEXT",
        "ALTER TABLE tasks ADD COLUMN receipt_id BLOB", 1)
    assert changed != sources["cea/store.py"]
    assert (_schema_signature({**sources, "cea/store.py": changed})
            != SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION])


@pytest.mark.parametrize("replacement", [
    '"parent_receipt_id": "BLOB",',
    '"parent_receipt_id": "TEXT",\n    "brand_new_col": "TEXT",',
])
def test_generated_receipt_columns_require_version_bump(tmp_path, replacement):
    TaskQueue(str(tmp_path / "tasks.db"))
    sources = _ddl_sources()
    changed = sources["cea/store.py"].replace(
        '"parent_receipt_id": "TEXT",', replacement, 1)
    assert changed != sources["cea/store.py"]
    assert (_schema_signature({**sources, "cea/store.py": changed})
            != SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION])
