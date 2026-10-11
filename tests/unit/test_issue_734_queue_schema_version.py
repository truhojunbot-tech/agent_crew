"""#734: a queue DDL edit requires an explicit protocol version bump."""

import ast
import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

from agent_crew.queue import QUEUE_SCHEMA_VERSION, TaskQueue
from agent_crew.server import create_app


# Update this map only when bumping QUEUE_SCHEMA_VERSION. Previous entries stay
# immutable so changing DDL under an existing version fails this test.
SCHEMA_SIGNATURES = {1: "c73fdf2908a7fb880a39d6157052f776481c96b3eed0d269bb09d537825b3b26"}
_QUEUE_SOURCE = Path(__file__).resolve().parents[2] / "src" / "agent_crew" / "queue.py"
_DDL_START = re.compile(r"^\s*(?:CREATE|ALTER|DROP)\s+", re.IGNORECASE)


def _schema_signature(source: str) -> str:
    """Fingerprint queue DDL declarations and inline schema statements."""
    tree = ast.parse(source)
    statements = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and (
                        target.id.startswith("_DDL") or target.id == "_EXEC_STATE_COLUMN_TYPES"):
                    statements.append((target.id, ast.dump(node.value, include_attributes=False)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _DDL_START.match(node.value):
                statements.append(("inline", node.value.strip()))
        elif isinstance(node, ast.JoinedStr):
            fragment = ast.get_source_segment(source, node) or ""
            if re.search(r"\b(?:CREATE|ALTER|DROP)\s+", fragment, re.IGNORECASE):
                statements.append(("inline", ast.dump(node, include_attributes=False)))
    payload = repr(sorted(statements)).encode()
    return hashlib.sha256(payload).hexdigest()


def test_health_exposes_queue_schema_version(tmp_path):
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="agent_crew",
                     push_fn=lambda *args, **kwargs: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["schema_version"] == QUEUE_SCHEMA_VERSION


def test_queue_ddl_requires_version_bump(tmp_path):
    source = _QUEUE_SOURCE.read_text()
    db = str(tmp_path / "tasks.db")
    TaskQueue(db)
    assert _schema_signature(source) == SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION]


def test_ddl_change_without_bump_fails_guard(tmp_path):
    source = _QUEUE_SOURCE.read_text()
    db = str(tmp_path / "tasks.db")
    TaskQueue(db)
    changed = source.replace("CREATE TABLE IF NOT EXISTS gates (",
                             "CREATE TABLE IF NOT EXISTS gates (\n    new_field TEXT,", 1)
    assert changed != source
    assert _schema_signature(changed) != SCHEMA_SIGNATURES[QUEUE_SCHEMA_VERSION]
