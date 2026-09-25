import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

from agent_crew.context_identity import CONTEXT_SCHEMA_VERSION
from agent_crew.protocol import (
    GateRequest, TaskRequest, TaskResult, RESULT_BRANCH_CONTEXT_KEY,
    RESULT_COMMIT_CONTEXT_KEY,
)
from agent_crew.telemetry import TaskTelemetry, TaskTelemetryAdapter, default_telemetry_adapter
from agent_crew.tokenomics_shadow import shadow_recommendation, shadow_recommendation_for_task_id
from agent_crew.risk_tier import RISK_DECLARATION_FIELDS, risk_declaration


class PausedError(Exception):
    """#314 P0-1: runtime STOP 활성 시 실행생성 mutation(enqueue)이 원자적으로 거부됐음을 알림.
    호출측(result cascade 등)은 이를 잡아 successor 생성 대신 suppression으로 처리한다."""
    pass

logger = logging.getLogger(__name__)


def _unknown_risk_declaration() -> dict:
    return {
        **{field: None for field in RISK_DECLARATION_FIELDS},
        "declaration_source": "unknown",
        "confidence": None,
    }


def _normalize_risk_declaration(value: object) -> dict:
    """Keep the serialized #342(C) declaration typed and nullable."""
    if not isinstance(value, dict):
        return _unknown_risk_declaration()
    normalized = {
        field: value.get(field) if isinstance(value.get(field), bool) else None
        for field in RISK_DECLARATION_FIELDS
    }
    source = value.get("declaration_source")
    confidence = value.get("confidence")
    normalized["declaration_source"] = (
        source if source in {"explicit", "heuristic", "unknown"} else "unknown"
    )
    normalized["confidence"] = confidence if confidence in {"high", "medium", "low"} else None
    return normalized


def _quality_evidence_envelope(quality_evidence: dict, risk: dict) -> dict:
    """Preserve #342(A)'s flat evidence contract with additive provenance.

    Existing consumers read the quota-core quality-evidence fields directly
    from ``evidence_json``.  Risk provenance is an additive key, not an
    envelope that relocates those deployed fields.
    """
    return {
        **quality_evidence,
        "risk_declaration": _normalize_risk_declaration(risk),
    }


class _TaskProviderSessionId(str):
    """Keep the adapter API stable while carrying a task-local boundary."""

    def __new__(cls, value: str, claude_transcript_start=None):
        result = super().__new__(cls, value)
        result.claude_transcript_start = claude_transcript_start
        return result

_ROLE_TO_TYPE = {
    "coder": "implement",
    "implementer": "implement",
    "reviewer": "review",
    "tester": "test",
    "panel": "discuss",
}

# Reverse map (canonical role name per task_type). Used by the server to
# pick which pane to push a new task to.
_TYPE_TO_ROLE = {
    "implement": "implementer",
    "review": "reviewer",
    "test": "tester",
    "discuss": "panel",
}

_DDL_GATES = """
CREATE TABLE IF NOT EXISTS gates (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    message    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
)
"""

_DDL_TOKENOMICS_SHADOW = """
CREATE TABLE IF NOT EXISTS tokenomics_shadow_receipts (
    task_id TEXT PRIMARY KEY, decision_source TEXT NOT NULL,
    policy_version TEXT, recommendation_json TEXT, actual_execution_json TEXT NOT NULL,
    economics_json TEXT, outcome TEXT,
    shadow_decision_source TEXT, shadow_policy_version TEXT,
    shadow_recommendation_json TEXT, shadow_contract_sha TEXT,
    shadow_resolved_at REAL, shadow_reason TEXT, evidence_json TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
)
"""

_DDL_MIGRATE_SHADOW_COLUMNS = (
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_decision_source TEXT",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_policy_version TEXT",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_recommendation_json TEXT",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_contract_sha TEXT",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_resolved_at REAL",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_reason TEXT",
    "ALTER TABLE tokenomics_shadow_receipts ADD COLUMN evidence_json TEXT",
)

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    description      TEXT NOT NULL,
    branch           TEXT NOT NULL DEFAULT '',
    priority         INTEGER NOT NULL DEFAULT 3,
    context          TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       REAL NOT NULL,
    project          TEXT NOT NULL DEFAULT '',
    summary          TEXT,
    verdict          TEXT,
    findings         TEXT,
    pr_number        INTEGER,
    last_activity_at REAL NOT NULL DEFAULT 0
)
"""

_DDL_MIGRATE_PROJECT = "ALTER TABLE tasks ADD COLUMN project TEXT NOT NULL DEFAULT ''"
_DDL_MIGRATE_LAST_ACTIVITY = (
    "ALTER TABLE tasks ADD COLUMN last_activity_at REAL NOT NULL DEFAULT 0"
)
_DDL_MIGRATE_PUSH_AT = "ALTER TABLE tasks ADD COLUMN push_at REAL NOT NULL DEFAULT 0"
_DDL_MIGRATE_DISPATCH_PANE = "ALTER TABLE tasks ADD COLUMN dispatch_pane_id TEXT NOT NULL DEFAULT ''"
_DDL_MIGRATE_ERROR_INFO = "ALTER TABLE tasks ADD COLUMN error_info TEXT DEFAULT NULL"

_DDL_ATTRIBUTION = """
CREATE TABLE IF NOT EXISTS task_attribution (
    task_id          TEXT PRIMARY KEY,
    project          TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT '',
    task_type        TEXT NOT NULL DEFAULT '',
    worktree_path    TEXT NOT NULL DEFAULT '',
    codex_logs_path  TEXT NOT NULL DEFAULT '',
    repo_url         TEXT NOT NULL DEFAULT '',
    git_branch       TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
)
"""

# Durable context identity (#202) — one row per (project, agent,
# worktree_path). NOT keyed by role: agent_override can route a task from
# one role into another agent's worktree, and doing so genuinely resumes
# that agent's ongoing provider conversation regardless of which role
# nominally owns the task (Agent ≠ Role ≠ Context).
_DDL_CONTEXT_STATE = """
CREATE TABLE IF NOT EXISTS context_state (
    context_key          TEXT PRIMARY KEY,
    project              TEXT NOT NULL DEFAULT '',
    role                 TEXT NOT NULL DEFAULT '',
    agent                TEXT NOT NULL DEFAULT '',
    worktree_path        TEXT NOT NULL DEFAULT '',
    context_id           TEXT NOT NULL,
    context_generation   INTEGER NOT NULL DEFAULT 1,
    session_task_index   INTEGER NOT NULL DEFAULT 0,
    provider_session_id  TEXT,
    last_task_id         TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL
)
"""

_DDL_COORDINATOR_STATE = """
CREATE TABLE IF NOT EXISTS coordinator_state (
    id INTEGER PRIMARY KEY CHECK (id = 1), project TEXT NOT NULL DEFAULT '',
    coordinator_id TEXT NOT NULL DEFAULT 'unknown',
    coordinator_generation INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'unknown', model TEXT NOT NULL DEFAULT 'unknown',
    provider_session_id TEXT NOT NULL DEFAULT 'unknown', checkpoint_ref TEXT NOT NULL DEFAULT '',
    previous_receipt_hash TEXT NOT NULL DEFAULT '', handoff_reason TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
)
"""

_DDL_COORDINATOR_RECEIPTS = """
CREATE TABLE IF NOT EXISTS coordinator_receipts (
    receipt_hash TEXT PRIMARY KEY, event TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
    coordinator_id TEXT NOT NULL DEFAULT 'unknown', coordinator_generation INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'unknown', model TEXT NOT NULL DEFAULT 'unknown',
    provider_session_id TEXT NOT NULL DEFAULT 'unknown', checkpoint_ref TEXT NOT NULL DEFAULT '',
    previous_receipt_hash TEXT NOT NULL DEFAULT '', handoff_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""

# task_attribution migrations (#202) — durable context identity + lineage
# fields, added via the same defensive ALTER-TABLE pattern as the existing
# tasks-table migrations below. All nullable/defaulted so existing rows
# (written before this migration) remain valid.
_DDL_MIGRATE_ATTR_SCHEMA_VERSION = (
    "ALTER TABLE task_attribution ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
)
_DDL_MIGRATE_ATTR_MODEL = "ALTER TABLE task_attribution ADD COLUMN model TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_CONTEXT_ID = "ALTER TABLE task_attribution ADD COLUMN context_id TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_PROVIDER_SESSION_ID = (
    "ALTER TABLE task_attribution ADD COLUMN provider_session_id TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_CONTEXT_POLICY = (
    "ALTER TABLE task_attribution ADD COLUMN context_policy TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_CONTEXT_GENERATION = (
    "ALTER TABLE task_attribution ADD COLUMN context_generation INTEGER DEFAULT 0"
)
_DDL_MIGRATE_ATTR_SESSION_TASK_INDEX = (
    "ALTER TABLE task_attribution ADD COLUMN session_task_index INTEGER DEFAULT 0"
)
_DDL_MIGRATE_ATTR_PREVIOUS_TASK_ID = (
    "ALTER TABLE task_attribution ADD COLUMN previous_task_id TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_RETRY_OF = "ALTER TABLE task_attribution ADD COLUMN retry_of TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_FALLBACK_OF = "ALTER TABLE task_attribution ADD COLUMN fallback_of TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_STARTED_AT = "ALTER TABLE task_attribution ADD COLUMN started_at REAL DEFAULT 0"
_DDL_MIGRATE_ATTR_COMPLETED_AT = "ALTER TABLE task_attribution ADD COLUMN completed_at REAL DEFAULT 0"
_DDL_MIGRATE_ATTR_OUTCOME = "ALTER TABLE task_attribution ADD COLUMN outcome TEXT DEFAULT ''"
# #278: tester economics. ⛔All five default to NULL, not '' / 0, which departs
#   from this table's convention on purpose. A historical row has no treatment,
#   and `effective_test_scope=''` or `lock_wait_seconds=0` would both read as
#   claims nobody made — "targeted" is not the default and "no lock wait" is
#   not the same as "never measured". NULL is the only value that says unknown.
_DDL_MIGRATE_ATTR_TEST_SCOPE = (
    "ALTER TABLE task_attribution ADD COLUMN effective_test_scope TEXT DEFAULT NULL")
_DDL_MIGRATE_ATTR_TEST_SCOPE_SOURCE = (
    "ALTER TABLE task_attribution ADD COLUMN test_scope_source TEXT DEFAULT NULL")
_DDL_MIGRATE_ATTR_TEST_SCOPE_HASH = (
    "ALTER TABLE task_attribution ADD COLUMN test_scope_hash TEXT DEFAULT NULL")
_DDL_MIGRATE_ATTR_LOCK_WAIT = (
    "ALTER TABLE task_attribution ADD COLUMN lock_wait_seconds REAL DEFAULT NULL")
_DDL_MIGRATE_ATTR_LOCK_DEFERS = (
    "ALTER TABLE task_attribution ADD COLUMN lock_defer_count INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_COORDINATOR_ID = "ALTER TABLE task_attribution ADD COLUMN coordinator_id TEXT NOT NULL DEFAULT 'unknown'"
_DDL_MIGRATE_ATTR_COORDINATOR_GENERATION = "ALTER TABLE task_attribution ADD COLUMN coordinator_generation INTEGER NOT NULL DEFAULT 0"
_DDL_MIGRATE_ATTR_COORDINATOR_PROVIDER = "ALTER TABLE task_attribution ADD COLUMN coordinator_provider TEXT NOT NULL DEFAULT 'unknown'"
_DDL_MIGRATE_ATTR_COORDINATOR_MODEL = "ALTER TABLE task_attribution ADD COLUMN coordinator_model TEXT NOT NULL DEFAULT 'unknown'"
_DDL_MIGRATE_ATTR_COORDINATOR_SESSION = "ALTER TABLE task_attribution ADD COLUMN coordinator_provider_session_id TEXT NOT NULL DEFAULT 'unknown'"
_DDL_MIGRATE_ATTR_COORDINATOR_CHECKPOINT = "ALTER TABLE task_attribution ADD COLUMN coordinator_checkpoint_ref TEXT NOT NULL DEFAULT ''"
_DDL_MIGRATE_ATTR_UNCACHED_INPUT = "ALTER TABLE task_attribution ADD COLUMN uncached_input_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_CACHE_WRITE = "ALTER TABLE task_attribution ADD COLUMN cache_write_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_CACHE_READ = "ALTER TABLE task_attribution ADD COLUMN cache_read_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_OUTPUT = "ALTER TABLE task_attribution ADD COLUMN output_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_REASONING = "ALTER TABLE task_attribution ADD COLUMN reasoning_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_CONTEXT_WINDOW = "ALTER TABLE task_attribution ADD COLUMN context_window_tokens INTEGER DEFAULT NULL"
_DDL_MIGRATE_ATTR_STABLE_PREFIX_HASH = "ALTER TABLE task_attribution ADD COLUMN stable_prefix_hash TEXT DEFAULT NULL"
_DDL_MIGRATE_ATTR_CONTEXT_PACK_HASH = "ALTER TABLE task_attribution ADD COLUMN context_pack_hash TEXT DEFAULT NULL"
_DDL_MIGRATE_ATTR_REQUIRED_CONTEXT_RECALLED = (
    "ALTER TABLE task_attribution ADD COLUMN required_context_recalled INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_SAFETY_OR_LIVE_CHANGE = (
    "ALTER TABLE task_attribution ADD COLUMN safety_or_live_change INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_BROAD_ARCHITECTURE_CHANGE = (
    "ALTER TABLE task_attribution ADD COLUMN broad_architecture_change INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_BOUNDED_ROUTINE_FIX = (
    "ALTER TABLE task_attribution ADD COLUMN bounded_routine_fix INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_HUMAN_GATE_REQUIRED = (
    "ALTER TABLE task_attribution ADD COLUMN human_gate_required INTEGER DEFAULT NULL")
_DDL_MIGRATE_ATTR_RISK_DECLARATION_SOURCE = (
    "ALTER TABLE task_attribution ADD COLUMN risk_declaration_source TEXT DEFAULT NULL")
_DDL_MIGRATE_ATTR_RISK_DECLARATION_CONFIDENCE = (
    "ALTER TABLE task_attribution ADD COLUMN risk_declaration_confidence TEXT DEFAULT NULL")

_DDL_MIGRATE_ATTRIBUTION_COLUMNS = (
    _DDL_MIGRATE_ATTR_SCHEMA_VERSION,
    _DDL_MIGRATE_ATTR_MODEL,
    _DDL_MIGRATE_ATTR_CONTEXT_ID,
    _DDL_MIGRATE_ATTR_PROVIDER_SESSION_ID,
    _DDL_MIGRATE_ATTR_CONTEXT_POLICY,
    _DDL_MIGRATE_ATTR_CONTEXT_GENERATION,
    _DDL_MIGRATE_ATTR_SESSION_TASK_INDEX,
    _DDL_MIGRATE_ATTR_PREVIOUS_TASK_ID,
    _DDL_MIGRATE_ATTR_RETRY_OF,
    _DDL_MIGRATE_ATTR_FALLBACK_OF,
    _DDL_MIGRATE_ATTR_STARTED_AT,
    _DDL_MIGRATE_ATTR_COMPLETED_AT,
    _DDL_MIGRATE_ATTR_OUTCOME,
    _DDL_MIGRATE_ATTR_TEST_SCOPE,
    _DDL_MIGRATE_ATTR_TEST_SCOPE_SOURCE,
    _DDL_MIGRATE_ATTR_TEST_SCOPE_HASH,
    _DDL_MIGRATE_ATTR_LOCK_WAIT,
    _DDL_MIGRATE_ATTR_LOCK_DEFERS,
    _DDL_MIGRATE_ATTR_COORDINATOR_ID,
    _DDL_MIGRATE_ATTR_COORDINATOR_GENERATION,
    _DDL_MIGRATE_ATTR_COORDINATOR_PROVIDER,
    _DDL_MIGRATE_ATTR_COORDINATOR_MODEL,
    _DDL_MIGRATE_ATTR_COORDINATOR_SESSION,
    _DDL_MIGRATE_ATTR_COORDINATOR_CHECKPOINT,
    _DDL_MIGRATE_ATTR_UNCACHED_INPUT,
    _DDL_MIGRATE_ATTR_CACHE_WRITE,
    _DDL_MIGRATE_ATTR_CACHE_READ,
    _DDL_MIGRATE_ATTR_OUTPUT,
    _DDL_MIGRATE_ATTR_REASONING,
    _DDL_MIGRATE_ATTR_CONTEXT_WINDOW,
    _DDL_MIGRATE_ATTR_STABLE_PREFIX_HASH,
    _DDL_MIGRATE_ATTR_CONTEXT_PACK_HASH,
    _DDL_MIGRATE_ATTR_REQUIRED_CONTEXT_RECALLED,
    _DDL_MIGRATE_ATTR_SAFETY_OR_LIVE_CHANGE,
    _DDL_MIGRATE_ATTR_BROAD_ARCHITECTURE_CHANGE,
    _DDL_MIGRATE_ATTR_BOUNDED_ROUTINE_FIX,
    _DDL_MIGRATE_ATTR_HUMAN_GATE_REQUIRED,
    _DDL_MIGRATE_ATTR_RISK_DECLARATION_SOURCE,
    _DDL_MIGRATE_ATTR_RISK_DECLARATION_CONFIDENCE,
)

_DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    checkpoint_num INTEGER NOT NULL,
    timestamp     REAL NOT NULL,
    state_snapshot TEXT NOT NULL,
    created_at    REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
    UNIQUE(task_id, checkpoint_num)
)
"""

_DDL_MIGRATE_STATUS_CHANGED_AT = (
    "ALTER TABLE tasks ADD COLUMN status_changed_at REAL DEFAULT 0"
)

_DDL_PR_ANNOUNCEMENTS = """
CREATE TABLE IF NOT EXISTS pr_announcements (
    pr_number  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    claimed_by TEXT NOT NULL DEFAULT '',
    claim_token TEXT NOT NULL DEFAULT '',
    posted_at  REAL,
    PRIMARY KEY (pr_number, kind)
)
"""

# #314 §1: DB-backed STOP epoch. 원자적 STOP 판단의 최종 authority는 이 단일행이다.
# pause.json은 부팅/외부 제어 신호(미러)일 뿐, claim/enqueue 원자결정의 단독 근거가 될 수 없다.
# epoch는 monotonic — pause/resume 전이마다 +1. STOP의 유일한 linearization point = 이 행의 commit.
_DDL_RUNTIME_STOP = """
CREATE TABLE IF NOT EXISTS runtime_stop (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    epoch      INTEGER NOT NULL DEFAULT 0,
    paused     INTEGER NOT NULL DEFAULT 0,
    incident   TEXT,
    note       TEXT,
    updated_at REAL NOT NULL DEFAULT 0
)
"""

# #314 §3/§4: cascade outbox. result 저장과 **항상 같은 txn**에 기록되는 durable continuation
# 레코드(result_json 전체). pause 여부와 무관하게 submit_result가 원자적으로 넣는다 → result 저장과
# suppression 기록 사이 crash로 continuation을 잃는 B3-b를 제거. §4: pause-aware executor가 lease
# (attempt_id/lease_owner/lease_expires_at)로 claim해 successor를 stable transition key로 생성하고
# state를 pending→replaying→applied로 CAS 전이한다(crash-safe at-most-once).
_DDL_CASCADE_OUTBOX = """
CREATE TABLE IF NOT EXISTS cascade_outbox (
    parent_task_id   TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL DEFAULT '',
    result_json      TEXT NOT NULL,
    stop_epoch       INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL DEFAULT 'pending',
    attempt_id       TEXT,
    lease_owner      TEXT,
    lease_expires_at REAL,
    created_at       REAL NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0
)
"""

# #314 §5: 외부 mutation(merge 등) idempotency receipt. DB txn을 외부 gh 호출과 공유할 수 없으므로
# exactly-once는 불가 — 대신 reservation(의도 기록) + 실제 GitHub 상태 재확인 + idempotent
# reconciliation으로 at-most-one-effect를 보장한다. crash(merge 후 done 기록 전)는 재기동 시
# op_key의 reserved를 보고 pr_state를 재확인해 이미-merged면 done만 기록(재merge 없음).
_DDL_EXTERNAL_OP = """
CREATE TABLE IF NOT EXISTS external_op (
    op_key        TEXT PRIMARY KEY,
    state         TEXT NOT NULL DEFAULT 'reserved',
    pr_number     INTEGER,
    attempt       INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    admitted_epoch INTEGER,
    reserved_at   REAL NOT NULL DEFAULT 0,
    done_at       REAL
)
"""
_DDL_MIGRATE_EXTERNAL_OP_ADMITTED = "ALTER TABLE external_op ADD COLUMN admitted_epoch INTEGER"

# Performance indexes for common queries
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task_num ON checkpoints(task_id, checkpoint_num DESC);
CREATE INDEX IF NOT EXISTS idx_gates_status ON gates(status);
"""


#: A task whose description OPENS with `Implement #<n>` is naming the issue it
#: implements. Anchored, and deliberately narrow — see `issue_from_description`.
_ISSUE_IN_DESCRIPTION = re.compile(r"^\s*implement\s+#(\d+)\b", re.IGNORECASE)


def issue_from_description(description) -> Optional[int]:
    """The issue number a task description names, or ``None`` (#276).

    #272 was implemented twice because dedup reads `context["issue"]` and the
    first task carried the number only here, in free text. This is the safety
    net for enqueue paths that do not populate the structured field.

    ⛔Anchored to the start, and to `implement` specifically. Measured over all
      5085 task rows on this host, 4721 of them without `context.issue`:

        ^implement #N          →   4 distinct numbers,    7 rows
        any #N minus "PR #N"   → 987 distinct numbers, 3829 rows
        "issue #N" anywhere    → 121 distinct numbers,  426 rows

      The broad rule matches spec section numbers, list markers and prose; at
      987 numbers it would stop the watcher claiming almost anything. The
      `issue #N` shape is dominated by *discuss* tasks, and a panel discussion
      is not work in flight on the issue. The anchored rule matched exactly the
      seven genuine rows and nothing else.

      The asymmetry is the argument. A miss costs one duplicate provider
      invocation; a false hit silently makes a real issue unclaimable for as
      long as the task is non-terminal. Prefer the miss.
    """
    if not isinstance(description, str):
        return None
    match = _ISSUE_IN_DESCRIPTION.match(description)
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _result_to_json(result) -> str:
    """TaskResult를 replay 재현 가능한 JSON 문자열로. pydantic model_dump 우선, 실패 시 __dict__.
    ⛔절대 'unknown' placeholder 금지 — cascade_outbox의 result_json은 executor가 원 제출 result를
      그대로 재실행하는 근거이므로 손실 없이 보존해야 한다(§3)."""
    try:
        data = result.model_dump()
    except Exception:
        data = getattr(result, "__dict__", None)
        if data is None:
            data = {"task_id": getattr(result, "task_id", None),
                    "status": getattr(result, "status", None)}
    return json.dumps(data, ensure_ascii=False, default=str)


def _is_duplicate_task_id(error: Exception) -> bool:
    """Is this IntegrityError the `tasks.task_id` primary-key conflict?

    Matches the constraint KIND and the COLUMN, not the whole message. Exact
    string equality would break on any SQLite wording change; matching only
    "unique constraint failed" would misreport a UNIQUE violation on some
    future column as a duplicate task — which is the same shape of mistake as
    catching every IntegrityError, one column later.
    """
    message = str(error).lower()
    return "unique constraint failed" in message and "tasks.task_id" in message


class TaskAlreadyExistsError(Exception):
    """Raised by :meth:`TaskQueue.enqueue` when ``task_id`` already exists.

    Carries the existing row's status so a caller (e.g. the ``/tasks`` HTTP
    handler) can report a 409 with enough detail to react, instead of the
    duplicate insert surfacing as a bare 500 (#273).
    """

    def __init__(self, task_id: str, status: str):
        self.task_id = task_id
        self.status = status
        super().__init__(f"task_id {task_id!r} already exists with status={status!r}")


def _is_issue_number(value) -> bool:
    """Is this a usable issue number?

    ⛔`bool` is a subclass of `int`, so a bare `isinstance(x, int)` accepts
      `True` and treats it as issue #1 — a real issue, which every
      `WHERE issue = ?` path then joins this task to. #270 lost a round to this
      once, and PR #295's own review found it a second time: the resolver
      rejected bools while `enqueue`'s gate did not, so a task the advisory
      resolved to #294 was persisted as `issue: true`. One predicate, so the
      two sites cannot answer differently again.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def task_issue_number(task) -> Optional[int]:
    """The issue a task is about — structured field first, description second.

    ⛔One resolver, because there are two call sites and they must agree.
      `TaskQueue.enqueue` backfills `context["issue"]` from an opening
      `Implement #N` description (#276), and `POST /tasks`'s in-flight advisory
      (#294) has to answer the same question BEFORE that write happens. Reading
      `context.issue` alone there meant a direct enqueue whose issue lived only
      in its description reported no collision and was then stored under that
      very issue — the advisory and the row it produced disagreeing about one
      task, and missing precisely the shape #276 exists for (review of PR #295).

    Structured precedence is unchanged: a description is free text, a context
    key is a claim, and when both are present the claim answers.
    """
    context = task.context if isinstance(getattr(task, "context", None), dict) else {}
    number = context.get("issue")
    if _is_issue_number(number):
        return number
    return issue_from_description(getattr(task, "description", None))


class TaskQueue:
    def __init__(self, db_path: str, *, telemetry_adapter: Optional[TaskTelemetryAdapter] = None,
                 read_only: bool = False):
        self._db_path = db_path
        self._telemetry_adapter = telemetry_adapter or default_telemetry_adapter()
        conn = self._connect()
        if read_only:
            # Status/reporting must not acquire schema/STOP authority merely
            # to inspect an already-initialized project database.
            conn.close()
            return
        conn.execute(_DDL)
        conn.execute(_DDL_GATES)
        conn.execute(_DDL_TOKENOMICS_SHADOW)
        for _stmt in _DDL_MIGRATE_SHADOW_COLUMNS:
            try:
                conn.execute(_stmt)
            except Exception:
                pass  # column already exists
        conn.execute(_DDL_ATTRIBUTION)
        conn.execute(_DDL_CHECKPOINTS)
        conn.execute(_DDL_CONTEXT_STATE)
        conn.execute(_DDL_COORDINATOR_STATE)
        conn.execute(_DDL_COORDINATOR_RECEIPTS)
        conn.execute(_DDL_PR_ANNOUNCEMENTS)
        conn.execute(_DDL_RUNTIME_STOP)
        conn.execute(_DDL_CASCADE_OUTBOX)
        conn.execute(_DDL_EXTERNAL_OP)
        try:
            conn.execute(_DDL_MIGRATE_EXTERNAL_OP_ADMITTED)
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE pr_announcements ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # column already exists
        # Migrate existing DBs: add project column if absent
        try:
            conn.execute(_DDL_MIGRATE_STATUS_CHANGED_AT)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_PROJECT)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_LAST_ACTIVITY)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_PUSH_AT)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_DISPATCH_PANE)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_ERROR_INFO)
        except Exception:
            pass  # column already exists
        # #202: durable context identity + lineage columns on task_attribution.
        for _stmt in _DDL_MIGRATE_ATTRIBUTION_COLUMNS:
            try:
                conn.execute(_stmt)
            except Exception:
                pass  # column already exists
        # Create indexes for performance
        for idx_stmt in _DDL_INDEXES.strip().split('\n'):
            if idx_stmt.strip():
                conn.execute(idx_stmt)
        conn.commit()
        conn.close()
        # #314 §1: boot reconciliation. pause.json(외부/부팅 신호)과 runtime_stop(DB 권위)을
        # 화해시킨다 — 더 높은 epoch가 승자, 같은 epoch인데 상태 불일치면 fail-closed(paused 유지).
        # restart-while-paused(기존 armed pause.json)가 새 코드에서 첫 요청부터 DB 게이트로 이어진다.
        self._reconcile_stop_on_boot()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── #309 K1: project coordinator handoff authority ──────────────────

    @staticmethod
    def _unknown(value: Optional[str]) -> str:
        """Keep absent coordinator attribution explicit rather than guessed."""
        return str(value).strip() if value is not None and str(value).strip() else "unknown"

    def _coordinator_project_on(self, conn) -> str:
        row = conn.execute(
            "SELECT project FROM tasks WHERE project <> '' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return (row["project"] if row else "") or ""

    def _coordinator_state_on(self, conn) -> dict:
        row = conn.execute("SELECT * FROM coordinator_state WHERE id=1").fetchone()
        if row is None:
            return {
                "project": self._coordinator_project_on(conn), "coordinator_id": "unknown",
                "coordinator_generation": 0, "provider": "unknown", "model": "unknown",
                "provider_session_id": "unknown", "checkpoint_ref": "",
                "previous_receipt_hash": "", "handoff_reason": "", "updated_at": 0.0,
            }
        return {key: row[key] for key in row.keys() if key != "id"}

    def get_coordinator_state(self) -> dict:
        """Read the project-local coordinator authority; missing is explicit unknown."""
        conn = self._connect()
        try:
            return self._coordinator_state_on(conn)
        finally:
            conn.close()

    @staticmethod
    def _coordinator_receipt_hash(event: str, state: dict) -> str:
        material = {"event": event, **state}
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def advance_coordinator(
        self, *, coordinator_id: str, generation: int, provider: str = "unknown",
        model: str = "unknown", provider_session_id: str = "unknown",
        handoff_reason: str = "", previous_receipt_hash: str = "",
    ) -> dict:
        """Atomically advance the durable coordinator generation (#309 K1).

        ``generation`` is an externally supplied compare-and-swap target.  A
        stale/equal proposal is quarantined as a receipt and cannot alter the
        authority, task rows, contexts, or worker lineage.  This mirrors the
        runtime_stop higher-generation-wins authority, but has no scheduler or
        fleet dependency.
        """
        try:
            requested = int(generation)
        except (TypeError, ValueError):
            requested = -1
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = self._coordinator_state_on(conn)
            current_generation = int(current["coordinator_generation"] or 0)
            now = time.time()
            project = current["project"] or self._coordinator_project_on(conn)
            if requested <= current_generation:
                rejected = {
                    "project": project, "coordinator_id": self._unknown(coordinator_id),
                    "coordinator_generation": requested, "provider": self._unknown(provider),
                    "model": self._unknown(model), "provider_session_id": self._unknown(provider_session_id),
                    "checkpoint_ref": "", "previous_receipt_hash": previous_receipt_hash or "",
                    "handoff_reason": handoff_reason or "", "updated_at": now,
                }
                receipt_hash = self._coordinator_receipt_hash("stale_generation_rejected", rejected)
                conn.execute(
                    "INSERT OR IGNORE INTO coordinator_receipts "
                    "(receipt_hash,event,project,coordinator_id,coordinator_generation,provider,model,provider_session_id,checkpoint_ref,previous_receipt_hash,handoff_reason,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_hash, "stale_generation_rejected", project, rejected["coordinator_id"],
                     requested, rejected["provider"], rejected["model"], rejected["provider_session_id"],
                     "", rejected["previous_receipt_hash"], rejected["handoff_reason"], now),
                )
                conn.execute("COMMIT")
                return {"accepted": False, "quarantined": True, "receipt_hash": receipt_hash,
                        "current": current, "reason": "stale coordinator_generation"}
            state = {
                "project": project, "coordinator_id": self._unknown(coordinator_id),
                "coordinator_generation": requested, "provider": self._unknown(provider),
                "model": self._unknown(model), "provider_session_id": self._unknown(provider_session_id),
                "checkpoint_ref": f"coordinator:{project}:{self._unknown(coordinator_id)}:{requested}",
                "previous_receipt_hash": previous_receipt_hash or "",
                "handoff_reason": handoff_reason or "", "updated_at": now,
            }
            receipt_hash = self._coordinator_receipt_hash("handoff_accepted", state)
            conn.execute(
                "INSERT INTO coordinator_state "
                "(id,project,coordinator_id,coordinator_generation,provider,model,provider_session_id,checkpoint_ref,previous_receipt_hash,handoff_reason,updated_at) "
                "VALUES (1,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET project=excluded.project, coordinator_id=excluded.coordinator_id, "
                "coordinator_generation=excluded.coordinator_generation, provider=excluded.provider, model=excluded.model, "
                "provider_session_id=excluded.provider_session_id, checkpoint_ref=excluded.checkpoint_ref, "
                "previous_receipt_hash=excluded.previous_receipt_hash, handoff_reason=excluded.handoff_reason, updated_at=excluded.updated_at",
                (state["project"], state["coordinator_id"], requested, state["provider"], state["model"],
                 state["provider_session_id"], state["checkpoint_ref"], state["previous_receipt_hash"],
                 state["handoff_reason"], now),
            )
            conn.execute(
                "INSERT INTO coordinator_receipts "
                "(receipt_hash,event,project,coordinator_id,coordinator_generation,provider,model,provider_session_id,checkpoint_ref,previous_receipt_hash,handoff_reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_hash, "handoff_accepted", state["project"], state["coordinator_id"], requested,
                 state["provider"], state["model"], state["provider_session_id"], state["checkpoint_ref"],
                 state["previous_receipt_hash"], state["handoff_reason"], now),
            )
            conn.execute("COMMIT")
            return {"accepted": True, "quarantined": False, "receipt_hash": receipt_hash, **state}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_coordinator_receipts(self, limit: int = 50) -> list:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM coordinator_receipts ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def export_project_runtime_state(self) -> dict:
        """A portable successor-coordinator snapshot; it never mutates runtime state."""
        conn = self._connect()
        try:
            tasks = []
            for row in conn.execute("SELECT * FROM tasks ORDER BY created_at ASC").fetchall():
                item = dict(row)
                item["context"] = json.loads(item["context"] or "{}")
                item["findings"] = json.loads(item["findings"] or "[]")
                item["error_info"] = json.loads(item["error_info"]) if item.get("error_info") else None
                tasks.append(item)
            receipts = []
            for row in conn.execute("SELECT * FROM task_attribution ORDER BY created_at ASC").fetchall():
                item = dict(row)
                item["worker_provider"] = item.pop("agent", "unknown") or "unknown"
                item["worker_provider_session_id"] = item.pop("provider_session_id", "unknown") or "unknown"
                item["worker_model"] = item.pop("model", "unknown") or "unknown"
                receipts.append(item)
            return {
                "schema_version": 1, "coordinator": self._coordinator_state_on(conn),
                "tasks": tasks, "worker_receipts": receipts,
                "gates": [dict(row) for row in conn.execute("SELECT * FROM gates ORDER BY created_at ASC").fetchall()],
                "claims": {
                    "cascade_outbox": [dict(row) for row in conn.execute("SELECT * FROM cascade_outbox ORDER BY created_at ASC").fetchall()],
                    "external_operations": [dict(row) for row in conn.execute("SELECT * FROM external_op ORDER BY reserved_at ASC").fetchall()],
                },
                "coordinator_receipts": self.list_coordinator_receipts(),
            }
        finally:
            conn.close()

    # ── #314 §1: DB-backed STOP epoch (authoritative) ────────────────────
    #
    # 원자적 STOP 판단의 최종 권위는 runtime_stop 단일행이다. enqueue/dequeue/discuss는
    # 자신의 BEGIN IMMEDIATE 트랜잭션 안에서 이 행을 읽어 게이트한다 → STOP writer의
    # commit이 이 트랜잭션의 SELECT 전에 끝나면 SQLite write 직렬화로 반드시 보인다
    # (file-read TOCTOU 제거). pause.json은 부팅/외부 신호로만 남고 원자결정 근거가 아니다.

    def _stop_dir(self) -> str:
        return os.path.dirname(self._db_path)

    def _read_stop_row(self, conn) -> dict:
        """runtime_stop 단일행을 읽어 정규화. 행 없음=기본 unpaused(부팅 reconcile이 seeding).
        읽기 예외는 호출측에서 fail-closed 처리."""
        row = conn.execute(
            "SELECT epoch, paused, incident, note, updated_at FROM runtime_stop WHERE id=1"
        ).fetchone()
        if row is None:
            return {"epoch": 0, "paused": False, "incident": None, "note": None, "updated_at": 0.0}
        return {"epoch": int(row["epoch"] or 0), "paused": bool(row["paused"]),
                "incident": row["incident"], "note": row["note"],
                "updated_at": row["updated_at"] or 0.0}

    def get_stop_epoch(self) -> dict:
        """현재 STOP 권위 상태 {epoch, paused, incident, note, updated_at} (관측/미러용)."""
        conn = self._connect()
        try:
            return self._read_stop_row(conn)
        finally:
            conn.close()

    def set_stop_epoch(self, paused: bool, incident: Optional[str] = None,
                       note: Optional[str] = None) -> int:
        """STOP 권위 전이. **DB에서 epoch를 먼저 +1 할당·commit**한다(§1: DB가 linearization point).
        cli pause/resume는 이 반환 epoch로 pause.json을 미러링해 두 소스의 generation을 일치시킨다.
        반환: 새 epoch."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = self._read_stop_row(conn)
            new_epoch = int(cur["epoch"]) + 1
            conn.execute(
                "INSERT INTO runtime_stop (id, epoch, paused, incident, note, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch, paused=excluded.paused, "
                "incident=excluded.incident, note=excluded.note, updated_at=excluded.updated_at",
                (new_epoch, 1 if paused else 0, incident, note, time.time()))
            conn.execute("COMMIT")
            return new_epoch
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def resume_stop(self, generation: int, incident: Optional[str] = None) -> dict:
        """generation-aware resume (DB 권위 CAS). `generation`은 새 epoch 목표이며 **현재 epoch보다
        커야** 한다 — 그렇지 않으면 stale resume으로 거부(그 사이 새 STOP이 epoch를 올렸을 수 있음).
        cli는 이 결과 epoch로 pause.json을 미러링한다. 반환: {resumed, epoch, reason}."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = self._read_stop_row(conn)
            if not cur["paused"]:
                conn.execute("ROLLBACK")
                # incident도 반환해 cli가 pause.json에 provenance를 mirror할 수 있게 한다.
                return {"resumed": True, "epoch": cur["epoch"], "reason": "not paused",
                        "incident": cur["incident"]}
            if int(generation) <= int(cur["epoch"]):
                conn.execute("ROLLBACK")
                return {"resumed": False, "epoch": cur["epoch"],
                        "reason": f"stale resume gen {generation} <= current epoch {cur['epoch']} — 거부",
                        "incident": cur["incident"], "still_paused": True}
            # DB는 resume 후에도 incident를 provenance로 보존한다(명시 incident 없으면 기존 유지).
            _kept_incident = incident if incident is not None else cur["incident"]
            conn.execute(
                "INSERT INTO runtime_stop (id, epoch, paused, incident, note, updated_at) "
                "VALUES (1, ?, 0, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch, paused=0, "
                "incident=excluded.incident, note=excluded.note, updated_at=excluded.updated_at",
                (int(generation), _kept_incident, "resumed via cli", time.time()))
            conn.execute("COMMIT")
            # cli는 이 incident를 pause.json에도 mirror해 두 소스의 provenance를 일치시킨다.
            return {"resumed": True, "epoch": int(generation), "reason": "resumed",
                    "incident": _kept_incident}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _pausejson_active(self) -> bool:
        """pause.json/global 외부 STOP 신호(additive). runtime_stop과 별개로, 직접 arm되거나
        global scope로 걸린 pause를 재시작 없이 라이브 큐에도 반영한다.

        ⛔이 신호는 **강화 전용(additive)** 이다 — 차단만 하고 admit은 못 한다. 따라서 원자
          linearization point는 여전히 runtime_stop(DB) 단독이고(unpause/admit은 DB로만 결정),
          pause.json을 여기서 읽어도 STOP을 약화시키는 원자성 구멍이 생기지 않는다.
          (리뷰어 v2 §2는 pause.json in-txn 제거를 요청했으나, 완전 제거 시 global/직접 pause가
           라이브 서버에 안 먹히는 회귀가 생긴다. DB 권위는 유지하되 이 신호는 additive로 남긴다 — 보고에 명시.)
        판정 불가는 fail-closed(=차단)."""
        try:
            from agent_crew import pause as _p
            return bool(_p.is_paused(self._stop_dir()))
        except Exception:
            return True  # fail-closed

    def _stop_active_in_txn(self, conn) -> bool:
        """호출자가 BEGIN IMMEDIATE로 write-lock을 쥔 상태에서 STOP을 확인.
        권위=runtime_stop(원자 linearization point) + additive pause.json 신호.
        읽기 실패(테이블 손상 등)는 fail-closed(=paused, 차단)."""
        try:
            row = conn.execute("SELECT paused FROM runtime_stop WHERE id=1").fetchone()
            db_paused = bool(row["paused"]) if row is not None else False
        except Exception:
            return True  # fail-closed
        if db_paused:
            return True
        return self._pausejson_active()

    def _stop_active_precheck(self) -> bool:
        """트랜잭션 밖 빠른 사전확인(권위 아님 — in-txn 재확인이 최종). 실패는 fail-closed."""
        try:
            if bool(self.get_stop_epoch()["paused"]):
                return True
        except Exception:
            return True
        return self._pausejson_active()

    def _reconcile_stop_on_boot(self) -> None:
        """§1 부팅 화해: pause.json(미러/외부신호) vs runtime_stop(DB권위).
        더 높은 epoch가 승자. 같은 epoch인데 (paused/incident)가 다르면 화해 불가 →
        fail-closed(paused 유지 + note에 conflict 기록). 손상/판정불가도 fail-closed.
        결과를 DB에 다시 기록해 수렴시킨다. (pause.json 미러 재기록은 cli 경로 소관.)"""
        state_dir = self._stop_dir()
        # pause.json 관측값(generation=epoch 미러). 실패는 fail-closed로 취급.
        pj_paused, pj_epoch, pj_incident, pj_ok = True, -1, None, False
        try:
            from agent_crew import pause as _pausemod
            st = _pausemod.pause_state(state_dir)
            pj_paused = bool(st.get("paused"))
            gens = []
            for scope_rec in (st.get("global"), st.get("project")):
                if isinstance(scope_rec, dict):
                    try:
                        gens.append(int(scope_rec.get("generation", 0) or 0))
                    except Exception:
                        gens.append(0)
            pj_epoch = max(gens) if gens else 0
            best = -1
            for s in st.get("active_scopes", []) or []:
                try:
                    g = int(s.get("generation", 0) or 0)
                except Exception:
                    g = 0
                if g >= best:
                    best, pj_incident = g, s.get("incident")
            pj_ok = True
        except Exception:
            pj_paused, pj_epoch, pj_incident, pj_ok = True, -1, None, False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            db = self._read_stop_row(conn)
            db_epoch, db_paused, db_incident = db["epoch"], db["paused"], db["incident"]
            note = None
            if not pj_ok:
                # pause.json 판정 불가 → fail-closed: paused 유지, epoch는 db 유지(+conflict note).
                win_paused, win_epoch, win_incident = True, max(db_epoch, 0), db_incident
                note = "boot-reconcile: pause.json 판정불가 → fail-closed paused"
            elif pj_epoch > db_epoch:
                win_paused, win_epoch, win_incident = pj_paused, pj_epoch, pj_incident
            elif db_epoch > pj_epoch:
                win_paused, win_epoch, win_incident = db_paused, db_epoch, db_incident
            else:
                # 같은 epoch — STOP 판단이 실제로 갈릴 때만 CONFLICT(fail-closed). incident는 STOP
                # 상태에서만 의미 있는 provenance이므로, 둘 다 unpaused면 incident mismatch는 conflict가
                # 아니다(canary#1이 잡은 버그: resume 후 pause.json incident=None vs db incident=set을
                # 같은 epoch에서 conflict로 오판 → 무한 fail-closed re-pause).
                if bool(pj_paused) != bool(db_paused):
                    # (1) paused 값 자체가 다름 → 화해 불가 → fail-closed
                    win_paused, win_epoch, win_incident = True, db_epoch, (db_incident or pj_incident)
                    note = (f"boot-reconcile CONFLICT: 같은 epoch({db_epoch}) paused 불일치 "
                            f"pj={pj_paused} vs db={db_paused} → fail-closed paused. 사람 개입 필요.")
                    logger.critical(note)
                elif db_paused and (pj_incident != db_incident):
                    # (2) 둘 다 paused=true인데 incident가 다름 → 화해 불가 → fail-closed
                    win_paused, win_epoch, win_incident = True, db_epoch, (db_incident or pj_incident)
                    note = (f"boot-reconcile CONFLICT: 같은 epoch({db_epoch}) 둘다 paused인데 incident "
                            f"불일치 pj={pj_incident} vs db={db_incident} → fail-closed paused. 사람 개입 필요.")
                    logger.critical(note)
                else:
                    # (3) 둘 다 unpaused(incident mismatch 무해) 또는 둘 다 paused+incident 일치 →
                    #     정상. DB incident를 provenance 기준으로 유지.
                    win_paused, win_epoch, win_incident = db_paused, db_epoch, db_incident
            conn.execute(
                "INSERT INTO runtime_stop (id, epoch, paused, incident, note, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch, paused=excluded.paused, "
                "incident=excluded.incident, note=excluded.note, updated_at=excluded.updated_at",
                (int(win_epoch), 1 if win_paused else 0, win_incident, note, time.time()))
            conn.execute("COMMIT")
        except Exception:
            # 화해 자체가 실패 → fail-closed로 paused 행을 남기려 시도(최후의 안전장치).
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                c2 = self._connect()
                c2.execute(
                    "INSERT INTO runtime_stop (id, epoch, paused, incident, note, updated_at) "
                    "VALUES (1, 0, 1, NULL, 'boot-reconcile 실패 → fail-closed paused', ?) "
                    "ON CONFLICT(id) DO UPDATE SET paused=1, "
                    "note='boot-reconcile 실패 → fail-closed paused', updated_at=excluded.updated_at",
                    (time.time(),))
                c2.commit()
                c2.close()
            except Exception:
                logger.critical("runtime_stop boot reconcile 및 fail-closed 기록 모두 실패")
        finally:
            conn.close()

    def enqueue(self, task: TaskRequest) -> str:
        # #276: make the structured field true at the choke point. Every path —
        # HTTP, MCP, pipeline, cli — arrives here, so backfilling once means
        # the read-side fallback in `watch.active_issue_numbers` rarely has to
        # fire, which is the right end state: a parser that is load-bearing on
        # free text will eventually be wrong.
        #
        # ⛔A copy, never the caller's dict. A caller may reuse the TaskRequest,
        #   and a backfill written through would be found by a later pass as if
        #   the caller had supplied it.
        context = dict(task.context or {})
        # ⛔The row stores the resolver's answer, full stop. Gating on
        #   `isinstance(..., int)` let `True` through — bool is a subclass of
        #   int — so a task the advisory had already resolved to #294 was
        #   persisted as `issue: true`, which is the advisory and the row
        #   disagreeing about one task: precisely the defect this PR closes
        #   (review of PR #295). A value that is not an issue number is dropped
        #   rather than left to join as issue #1.
        resolved = task_issue_number(task)
        if resolved is not None:
            context["issue"] = resolved
        elif "issue" in context and not _is_issue_number(context["issue"]):
            context.pop("issue")
        conn = self._connect()
        try:
            # #313/#314 P0-1: 실행생성 mutation(INSERT) 자체를 pause와 원자적으로 admission.
            # BEGIN IMMEDIATE로 write-lock을 쥔 뒤 pause를 확인한다 → submit_result의 사전 체크와
            # commit 사이에 STOP이 authoritative가 됐어도 여기서 잡혀 successor가 생성되지 않는다.
            conn.execute("BEGIN IMMEDIATE")
            # #314 §2: 권위는 runtime_stop 행. 같은 write-lock 트랜잭션에서 읽으므로
            # STOP writer의 commit이 이 INSERT 전에 끝났으면 반드시 보인다(TOCTOU 제거).
            if self._stop_active_in_txn(conn):
                conn.execute("ROLLBACK")
                raise PausedError(f"enqueue blocked by runtime STOP (task_id={task.task_id})")
            conn.execute(
                """
                INSERT INTO tasks (task_id, task_type, description, branch, priority, context, status, created_at, project)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task.task_id,
                    task.task_type,
                    task.description,
                    task.branch,
                    task.priority,
                    json.dumps(context),
                    time.time(),
                    task.project,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # #273: task_id is the primary key, so a duplicate insert raises
            # here. Report the existing row's status rather than letting the
            # bare IntegrityError surface as an opaque 500 with no detail.
            conn.rollback()
            if not _is_duplicate_task_id(e):
                # ⛔Only the task_id conflict is a duplicate. `tasks` has six
                #   NOT NULL columns, and the first version of this catch
                #   reported a NOT NULL violation as "already exists" with
                #   status='unknown' (the follow-up SELECT found nothing,
                #   because nothing was there). That is worse than the 500 it
                #   replaced: a caller told "already exists" stops and treats
                #   the work as in flight, where a caller told "the insert
                #   failed" retries or escalates. A write bug turned into a
                #   false duplicate loses the task silently (review of PR #274).
                raise
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()
            existing_status = row["status"] if row is not None else "unknown"
            raise TaskAlreadyExistsError(task.task_id, existing_status) from None
        finally:
            conn.close()
        # #342(C): risk declaration is strictly post-commit telemetry.  Even
        # an unexpected classifier exception can never veto the task INSERT,
        # and a failure leaves the declaration honestly unknown.
        declaration = _unknown_risk_declaration()
        try:
            declaration = _normalize_risk_declaration(
                risk_declaration(task.description, context)
            )
            self.patch_context(task.task_id, {"risk_declaration": declaration})
        except Exception:
            logger.exception("risk declaration telemetry failed after enqueue for %s", task.task_id)
        # Shadow telemetry is strictly post-commit and best-effort: a policy
        # reader, receipt write, or future adapter bug can never veto domain
        # admission (including through an unfamiliar exception class).
        try:
            shadow = shadow_recommendation(task)
            now = time.time()
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO tokenomics_shadow_receipts
                       (task_id, decision_source, policy_version, recommendation_json,
                        actual_execution_json, evidence_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task.task_id, shadow["decision_source"], shadow["policy_version"],
                     json.dumps(shadow["recommendation"]),
                     json.dumps({"cascade": "baseline", "task_type": task.task_type,
                                 "shadow_reason": shadow["reason"]}),
                     json.dumps(_quality_evidence_envelope({
                         "outcome": None, "independent_review_correct": None,
                         "required_context_recalled": None, "context_growth_tokens": None,
                         "retry_of": None, "fallback_of": None,
                         "token_observations": {
                             "uncached_input_tokens": None, "cache_write_tokens": None,
                             "cache_read_tokens": None, "output_tokens": None,
                             "reasoning_tokens": None,
                         },
                     }, declaration)), now, now),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("tokenomics shadow receipt failed after enqueue for %s", task.task_id)
        return task.task_id

    # ── one-shot PR announcements (#250 review) ───────────────────────
    #
    # ⛔The claim is a ROW, not a check. The exhaustion notice used to do
    #   "does the PR already have this comment?" then post, and two results
    #   completing together both read "no" and both posted — the same
    #   check-then-act that could not be used for the fix task id in #244, for
    #   the same reason. The PRIMARY KEY below is the arbiter: exactly one
    #   caller wins the insert, across threads AND processes sharing this file.

    #: An unposted claim older than this is assumed to belong to a process that
    #: died between claiming and posting, and may be taken over. Without it a
    #: crash in that window would suppress the escalation permanently.
    PR_ANNOUNCEMENT_STALE_AFTER = 300.0

    def claim_pr_announcement(self, pr_number: int, kind: str, *,
                              claimed_by: str = "",
                              stale_after: float = PR_ANNOUNCEMENT_STALE_AFTER):
        """Win the right to post `kind` on `pr_number` exactly once.

        Returns an opaque **claim token** to exactly one caller, or ``None``.
        Every later operation on the row must present that token.

        ⛔The token is a fencing token, and without it the lease is unsafe. A
          takeover after `stale_after` does not stop the previous owner from
          still running: if A is merely slow rather than dead, B reclaims, and
          then A — unaware — marks B's claim posted, or releases it. Both let a
          second notice reach the PR, which is exactly what this table exists to
          prevent (review of PR #251, round 3). Ownership is therefore checked
          in SQL on every write, not assumed from having once held the claim.
        """
        token = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT claimed_at, posted_at FROM pr_announcements "
                "WHERE pr_number=? AND kind=?", (int(pr_number), kind)).fetchone()
            now = time.time()
            if row is not None:
                if row["posted_at"] is not None:
                    conn.rollback()
                    return None
                if now - (row["claimed_at"] or 0) < stale_after:
                    conn.rollback()
                    return None
                conn.execute(
                    "UPDATE pr_announcements SET claimed_at=?, claimed_by=?, claim_token=? "
                    "WHERE pr_number=? AND kind=? AND posted_at IS NULL",
                    (now, claimed_by, token, int(pr_number), kind))
            else:
                conn.execute(
                    "INSERT INTO pr_announcements "
                    "(pr_number, kind, claimed_at, claimed_by, claim_token) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(pr_number), kind, now, claimed_by, token))
            conn.commit()
            return token
        except sqlite3.IntegrityError:
            # A concurrent writer won the insert. That is the mechanism working.
            return None
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def owns_pr_announcement(self, pr_number: int, kind: str, token: str) -> bool:
        """Do we still hold this claim, and is it still unposted?

        Called immediately before the external side effect. A lease-expired
        worker must not post: it cannot un-post a comment afterwards, and the
        row it would update no longer belongs to it.
        """
        if not token:
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM pr_announcements WHERE pr_number=? AND kind=? "
                "AND claim_token=? AND posted_at IS NULL",
                (int(pr_number), kind, token)).fetchone()
            return row is not None
        finally:
            conn.close()

    def mark_pr_announcement_posted(self, pr_number: int, kind: str,
                                    token: str = "") -> bool:
        """Record that the notice was published. Returns whether it applied.

        Conditional on ownership: a worker whose lease was taken over cannot
        mark the NEW owner's claim as posted and silence it.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE pr_announcements SET posted_at=? "
                "WHERE pr_number=? AND kind=? AND claim_token=? AND posted_at IS NULL",
                (time.time(), int(pr_number), kind, token))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def release_pr_announcement(self, pr_number: int, kind: str,
                                token: str = "") -> bool:
        """Give the claim back after a failed post. Returns whether it applied.

        ⛔Two conditions, and both are load-bearing. Only an UNPOSTED claim is
          released — clearing a posted one would let the notice be published
          twice. And only OUR claim: a worker whose lease expired must not
          delete the live claim of whoever took over, which would hand a third
          worker the right to post alongside them.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM pr_announcements WHERE pr_number=? AND kind=? "
                "AND claim_token=? AND posted_at IS NULL",
                (int(pr_number), kind, token))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def pr_announcement_state(self, pr_number: int, kind: str):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT pr_number, kind, claimed_at, claimed_by, posted_at "
                "FROM pr_announcements WHERE pr_number=? AND kind=?",
                (int(pr_number), kind)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def patch_context(self, task_id: str, extra: dict) -> None:
        """Merge ``extra`` into the existing context of a pending task."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            existing = json.loads(row["context"] or "{}")
            merged = {**existing, **extra}
            conn.execute(
                "UPDATE tasks SET context=? WHERE task_id=?",
                (json.dumps(merged), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def merge_task_context(self, task_id: str, updates: dict) -> None:
        """Best-effort-safe JSON merge for result metadata written after submit.

        This intentionally has no STOP admission or cascade behaviour: callers
        use it only after ``submit_result`` committed the terminal result.
        """
        if not updates:
            return
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET context = json_patch(context, ?) WHERE task_id = ?",
                (json.dumps(updates), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def dequeue(self, agent: str = "", role: str = "") -> Optional[TaskRequest]:
        """Atomically dequeue the next pending task for ``agent`` / ``role``.

        Resolution order (Issue #106 phase 3 — supports dynamic role
        reassignment via ``context.agent_override``):

        1. ``agent`` given: prefer tasks whose ``context.agent_override``
           equals ``agent``, regardless of task_type. Operator overrides
           (``crew run --reviewer gemini``) and the rate-limit fallback
           chain both flow through this path.
        2. ``role`` given (with or without ``agent``): tasks of the
           matching task_type WHERE either no override is set or the
           override claims this agent. The latter clause prevents an
           agent from stealing a task explicitly routed to another
           agent. Stage 2 only runs after stage 1 has no candidate.
        3. Neither given: any pending task, ordered by priority.
        """
        # #311/#314 STOP 전파: 런타임 STOP 활성이면 어떤 task도 claim/start하지 않는다.
        # 이 한 지점이 tmux push(_try_push_next)와 MCP GET /tasks/next를 모두 덮어
        # 큐 드레인·successor/retry stage 시작을 막는다. in-flight는 자기 원자단위까지만.
        # 사전확인은 빠른 fail용(권위 아님) — 최종 판단은 아래 in-txn 재확인.
        if self._stop_active_precheck():
            return None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # #314 §2: 권위는 runtime_stop 행. write-lock 임계구역 안에서 재확인한다.
            # 사전확인과 commit 사이에 STOP이 authoritative가 됐어도 여기서 잡혀
            # 어떤 task도 pending->in_progress로 커밋되지 않는다(TOCTOU 제거). fail-closed.
            if self._stop_active_in_txn(conn):
                conn.execute("ROLLBACK")
                return None
            row = None
            if agent:
                # Stage 1 — explicit override claim for this agent.
                row = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'pending'
                      AND json_extract(context, '$.agent_override') = ?
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    """,
                    (agent,),
                ).fetchone()

            if row is None and role:
                # Stage 2 — default role, skipping tasks claimed by others.
                task_type_filter = _ROLE_TO_TYPE.get(role)
                if task_type_filter is None:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"Unknown role: {role!r}. Must be one of {list(_ROLE_TO_TYPE)}")
                if agent:
                    row = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE status = 'pending' AND task_type = ?
                          AND (
                            json_extract(context, '$.agent_override') IS NULL
                            OR json_extract(context, '$.agent_override') = ?
                          )
                        ORDER BY priority ASC, created_at ASC
                        LIMIT 1
                        """,
                        (task_type_filter, agent),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE status = 'pending' AND task_type = ?
                        ORDER BY priority ASC, created_at ASC
                        LIMIT 1
                        """,
                        (task_type_filter,),
                    ).fetchone()

            if row is None and not agent and not role:
                row = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'pending'
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    """
                ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return None

            conn.execute(
                "UPDATE tasks SET status = 'in_progress', last_activity_at = ? WHERE task_id = ?",
                (time.time(), row["task_id"]),
            )
            conn.execute("COMMIT")

            return TaskRequest(
                task_id=row["task_id"],
                task_type=row["task_type"],
                description=row["description"],
                branch=row["branch"],
                priority=row["priority"],
                context=json.loads(row["context"]),
                project=row["project"] if row["project"] else "",
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def submit_result(self, task_id: str, result: TaskResult) -> str:
        """Submit a task result. Returns the task_type of the completed task
        (so push-model callers can decide what to push next)."""
        conn = self._connect()
        try:
            if result.task_id != task_id:
                raise ValueError(f"task_id mismatch: argument {task_id!r} != result.task_id {result.task_id!r}")
            # Read the provider's own transcript before the terminal write. The
            # adapter boundary is deliberately provider-neutral here; failure
            # to observe a transcript is represented by NULL, never a guess.
            telemetry = self._extract_task_telemetry(conn, task_id)
            # #314 §3: result 저장·outbox 기록·pause 판단을 하나의 write-lock 트랜잭션으로 원자화한다.
            # 이렇게 해야 outbox의 state(pending=억제 / applied=라이브처리)가 결과 저장 시점의 STOP
            # 상태와 원자적으로 확정돼, 서버가 별도로 pause를 재확인하며 생기는 divergence가 사라진다.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status FROM tasks WHERE task_id = ?",
                (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Task not found: {task_id!r}")
            if row["status"] == "cancelled":
                raise ValueError(
                    f"LATE_RESULT_REJECTED: cancelled task {task_id!r} cannot accept a result"
                )
            task_type = row["task_type"]
            self._last_previous_status = row["status"]
            # #167: persist structured error_info for failed results so post-mortem
            # debugging has machine-readable data, not just the free-form summary.
            # #265: `timed_out` too — a consumer that sees "we stopped waiting"
            # needs the reason as badly as one that sees "it failed", and leaving
            # the field null there is exactly what made the cause unreadable.
            error_info_json = None
            if result.status in ("failed", "timed_out") and result.error_info:
                error_info_json = json.dumps(result.error_info)
            # ⛔`status_changed_at` moves only when the status actually moves.
            #   Stamping it on every submission made a duplicate same-status
            #   POST look like a revision, and the whole point of the field is
            #   that a consumer can read it as "the verdict I saw was revised"
            #   (#265). A field that fires when nothing changed is worse than no
            #   field: it manufactures exactly the false signal it was added to
            #   remove. The CASE compares against the stored value inside the
            #   same statement, so a concurrent writer cannot slip between a
            #   read and a write.
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, summary = ?, verdict = ?, findings = ?, pr_number = ?,
                    error_info = ?,
                    status_changed_at = CASE WHEN status = ? THEN status_changed_at
                                             ELSE ? END
                WHERE task_id = ?
                """,
                (
                    result.status,
                    result.summary,
                    result.verdict,
                    json.dumps(result.findings),
                    result.pr_number,
                    error_info_json,
                    result.status,
                    time.time(),
                    task_id,
                ),
            )
            # #202: record the final outcome on the attribution row too, in
            # the same transaction, so it's set regardless of whether this
            # came from the agent's own POST /tasks/{id}/result or an
            # internal dispatcher failure path (_fail_if_active calls this
            # method directly) — both funnel through here. status is set
            # alongside outcome/completed_at (not left at 'in_progress') so
            # the row can't end up internally contradictory — a terminal
            # outcome sitting next to a stale in_progress status broke
            # status-based external queries (review of PR #203, finding 3).
            outcome = result.status
            if result.status == "failed" and isinstance(result.error_info, dict):
                reason = result.error_info.get("reason")
                if reason:
                    outcome = f"failed:{reason}"
            now = time.time()
            conn.execute(
                "UPDATE task_attribution SET status=?, outcome=?, completed_at=?, updated_at=? WHERE task_id=?",
                (result.status, outcome, now, now, task_id),
            )
            self._store_task_telemetry(conn, task_id, telemetry, now)
            # #204: completed_at >= started_at is expected to always hold —
            # started_at is set once at first dispatch and never rewritten
            # (see record_attribution). It can only be violated by clock
            # skew or a corrupted row; surface that explicitly rather than
            # silently handing a downstream consumer a negative-duration
            # window.
            attr_row = conn.execute(
                "SELECT started_at FROM task_attribution WHERE task_id=?", (task_id,)
            ).fetchone()
            if attr_row is not None:
                started_at = attr_row["started_at"]
                if started_at and started_at > now:
                    logger.warning(
                        f"task_attribution timing invariant violated for {task_id!r}: "
                        f"completed_at={now} < started_at={started_at}"
                    )
            # #314 §3: result 저장과 **같은 txn**에 cascade_outbox 원자 insert(같은 write-lock).
            # state는 이 시점 STOP에 따라 원자적으로 결정된다:
            #   paused  → 'pending'  (억제됨 — pause-aware executor(§4)가 재개 후 drain)
            #   unpaused→ 'applied'  (라이브 cascade가 동기 처리 — 부팅 executor가 재실행 안 함)
            # 이로써 "result 저장→crash→suppression 기록 전 continuation 유실"(B3-b)이 제거되고,
            # 서버는 outbox state만 보면 되어 pause 재확인 divergence가 사라진다.
            # INSERT OR IGNORE: 이미 있으면(replay 재호출) 기존 state 보존(pending/applied 안 뒤집음).
            # ⛔실패를 삼키지 않는다 — insert 실패 시 txn 전체 rollback되어 result도 미저장(원자성).
            _stop = self._read_stop_row(conn)
            # 게이트와 동일한 union: runtime_stop(원자 권위) OR pause.json(additive fail-closed).
            _suppressed = bool(_stop["paused"]) or self._pausejson_active()
            self._last_cascade_suppressed = _suppressed
            self._last_stop_epoch = int(_stop["epoch"])
            conn.execute(
                "INSERT OR IGNORE INTO cascade_outbox "
                "(parent_task_id, task_type, result_json, stop_epoch, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, task_type, _result_to_json(result), int(_stop["epoch"]),
                 "pending" if _suppressed else "applied", now, now))
            conn.commit()
            # Completion-time recommendations are intentionally a separate,
            # post-commit receipt.  Admission's decision fields are immutable:
            # a later quota-core report must not rewrite what was known when
            # the task entered the queue.
            try:
                self._refresh_shadow_after_commit(task_id, outcome)
            except Exception:
                logger.exception("tokenomics completion shadow refresh failed for %s", task_id)
            return task_type
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _extract_task_telemetry(self, conn: sqlite3.Connection, task_id: str) -> TaskTelemetry:
        row = conn.execute(
            """
            SELECT a.agent, a.worktree_path, a.provider_session_id, t.context
            FROM task_attribution AS a JOIN tasks AS t ON t.task_id=a.task_id
            WHERE a.task_id=?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return TaskTelemetry()
        try:
            try:
                context = json.loads(row["context"] or "{}")
            except (TypeError, ValueError):
                context = {}
            boundary = (context.get("claude_transcript_start")
                        if isinstance(context, dict) else None)
            return self._telemetry_adapter.extract(
                provider=str(row["agent"] or ""),
                worktree_path=str(row["worktree_path"] or ""),
                provider_session_id=_TaskProviderSessionId(
                    str(row["provider_session_id"] or ""), boundary),
            )
        except Exception:
            logger.exception("task telemetry adapter failed for %s", task_id)
            return TaskTelemetry()

    def record_task_telemetry(self, task_id: str, telemetry: TaskTelemetry) -> None:
        """Persist terminal provider-response observations after process exit."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._store_task_telemetry(conn, task_id, telemetry, now)
            conn.commit()
            # #334 may write final provider usage after submit_result. Refresh
            # the shadow receipt only after that telemetry is durable.
            try:
                self._refresh_shadow_after_commit(task_id)
            except Exception:
                logger.exception("late tokenomics completion shadow refresh failed for %s", task_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _refresh_shadow_economics(conn: sqlite3.Connection, task_id: str, now: float,
                                  outcome: Optional[str] = None) -> None:
        economics = conn.execute(
            """SELECT uncached_input_tokens, cache_write_tokens, cache_read_tokens,
                      output_tokens, reasoning_tokens, context_window_tokens,
                      outcome, retry_of, fallback_of, required_context_recalled
               FROM task_attribution WHERE task_id=?""", (task_id,)).fetchone()
        token_observations = {
            key: (economics[key] if economics is not None else None)
            for key in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                        "output_tokens", "reasoning_tokens")
        }
        # quota-core #80 evidence contract: NULL is an observation of unknown,
        # never an inferred false/zero.  `required_context_recalled` is written
        # only by the Context Pack producer; other quality facts remain NULL
        # until Agent Crew has a direct observation for them (#342 A).
        evidence = {
            "outcome": outcome or (economics["outcome"] if economics is not None and "outcome" in economics.keys() else None),
            "independent_review_correct": None,
            "required_context_recalled": (
                None if economics is None else
                (None if economics["required_context_recalled"] is None
                 else bool(economics["required_context_recalled"]))),
            "context_growth_tokens": None,
            "retry_of": (economics["retry_of"] or None) if economics is not None and "retry_of" in economics.keys() else None,
            "fallback_of": (economics["fallback_of"] or None) if economics is not None and "fallback_of" in economics.keys() else None,
            "token_observations": token_observations,
        }
        task_row = conn.execute(
            "SELECT context FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        try:
            task_context = json.loads(task_row["context"] or "{}") if task_row is not None else {}
        except (TypeError, ValueError):
            task_context = {}
        conn.execute(
            """UPDATE tokenomics_shadow_receipts
               SET outcome=COALESCE(?, outcome), economics_json=?, evidence_json=?, updated_at=? WHERE task_id=?""",
            (outcome, json.dumps(dict(economics)) if economics is not None else None,
             json.dumps(_quality_evidence_envelope(
                 evidence,
                 task_context.get("risk_declaration") if isinstance(task_context, dict) else None,
             )), now, task_id),
        )

    def record_required_context_recalled(self, task_id: str, observed: Optional[bool]) -> None:
        """Persist Context Pack recall evidence without converting unknown to false (#342)."""
        if observed is not None and not isinstance(observed, bool):
            raise TypeError("required_context_recalled must be bool or None")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE task_attribution SET required_context_recalled=?, updated_at=? WHERE task_id=?",
                (None if observed is None else int(observed), time.time(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _refresh_shadow_after_commit(self, task_id: str, outcome: Optional[str] = None) -> None:
        """Best-effort completion-time policy observation (#342 Option A).

        This deliberately opens a new transaction after result/telemetry has
        committed.  It can therefore never roll back admission or task
        completion, and it records a later recommendation in separate fields
        rather than laundering the immutable admission-time receipt.
        """
        shadow = shadow_recommendation_for_task_id(task_id)
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._refresh_shadow_economics(conn, task_id, now, outcome)
            conn.execute(
                """UPDATE tokenomics_shadow_receipts
                   SET shadow_decision_source=?, shadow_policy_version=?,
                       shadow_recommendation_json=?, shadow_contract_sha=?,
                       shadow_resolved_at=?, shadow_reason=?, updated_at=?
                   WHERE task_id=?""",
                (shadow["decision_source"], shadow["policy_version"],
                 (json.dumps(shadow["recommendation"])
                  if shadow["recommendation"] is not None else None), shadow.get("contract_sha"),
                 now, shadow["reason"], now, task_id),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _store_task_telemetry(
        conn: sqlite3.Connection, task_id: str, telemetry: TaskTelemetry, now: float
    ) -> None:
        """Persist only observed values so absent provider fields remain NULL."""
        context_pack_hash = telemetry.context_pack_hash
        if context_pack_hash is None:
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            try:
                context = json.loads(row["context"] or "{}") if row is not None else {}
            except (TypeError, ValueError):
                context = {}
            if isinstance(context, dict) and isinstance(context.get("context_pack_hash"), str):
                context_pack_hash = context["context_pack_hash"]
        fields = (
            "uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
            "output_tokens", "reasoning_tokens", "context_window_tokens",
            "stable_prefix_hash",
        )
        values = [getattr(telemetry, field) for field in fields]
        observed = [(field, value) for field, value in zip(fields, values) if value is not None]
        if context_pack_hash is not None:
            observed.append(("context_pack_hash", context_pack_hash))
        assignments = [f"{field}=?" for field, _ in observed]
        params = [value for _, value in observed]
        # Dispatch attribution is authoritative when it named a model/session;
        # a transcript fills only an absent value, never rewrites lineage.
        if telemetry.model:
            assignments.append("model=CASE WHEN model IN ('', 'unknown') THEN ? ELSE model END")
            params.append(telemetry.model)
        if telemetry.provider_session_id:
            assignments.append(
                "provider_session_id=CASE WHEN provider_session_id IN ('', 'unknown') THEN ? ELSE provider_session_id END"
            )
            params.append(telemetry.provider_session_id)
        if not assignments:
            return
        conn.execute(
            f"UPDATE task_attribution SET {', '.join(assignments)}, updated_at=? WHERE task_id=?",
            params + [now, task_id],
        )
        # A transcript path proven from a fresh task's dispatch snapshot is a
        # provider-native session binding just like one parsed from CLI output.
        # Keep it on the exact context generation that ran this task, so the
        # next dispatch can take an ordinary byte-offset boundary.
        if telemetry.provider_session_id:
            conn.execute(
                """
                UPDATE context_state SET provider_session_id=?, updated_at=?
                WHERE context_id=(SELECT context_id FROM task_attribution WHERE task_id=?)
                """,
                (telemetry.provider_session_id, now, task_id),
            )

    # ── #314 §4: cascade outbox executor primitives (lease + CAS) ─────────
    #
    # executor는 pending(및 만료된 replaying) 행을 lease로 claim → stored result_json으로
    # cascade를 재실행(server 측 _run_result_cascade) → applied로 CAS. crash한 replaying은
    # lease 만료 후 다른 owner가 안전하게 reclaim(at-most-once는 successor stable id로 보장).

    #: replaying lease 유효기간. 이보다 오래된 replaying은 crash한 owner로 간주해 회수한다.
    CASCADE_LEASE_TTL = 300.0

    def outbox_pending(self, include_replaying: bool = True) -> List[dict]:
        """처리 대기(pending) + (옵션)회수 대상 replaying 행 목록."""
        conn = self._connect()
        try:
            if include_replaying:
                rows = conn.execute(
                    "SELECT * FROM cascade_outbox WHERE state IN ('pending','replaying') "
                    "ORDER BY created_at ASC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cascade_outbox WHERE state='pending' ORDER BY created_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def outbox_get(self, parent_task_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM cascade_outbox WHERE parent_task_id=?", (parent_task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def outbox_claim(self, parent_task_id: str, owner: str,
                     ttl: Optional[float] = None) -> Optional[dict]:
        """lease CAS로 outbox 행을 claim. pending이거나 **만료된 replaying**(crash 회수)일 때만
        성공. rowcount=1인 claimer만 진행(동시 replay dedup). 반환: claim한 행 dict 또는 None."""
        ttl = self.CASCADE_LEASE_TTL if ttl is None else ttl
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            attempt = uuid.uuid4().hex
            cur = conn.execute(
                "UPDATE cascade_outbox SET state='replaying', lease_owner=?, attempt_id=?, "
                "lease_expires_at=?, updated_at=? "
                "WHERE parent_task_id=? AND (state='pending' "
                "   OR (state='replaying' AND (lease_expires_at IS NULL OR lease_expires_at < ?)))",
                (owner, attempt, now + ttl, now, parent_task_id, now))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            row = conn.execute(
                "SELECT * FROM cascade_outbox WHERE parent_task_id=?", (parent_task_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return dict(row) if row else None
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def outbox_mark_applied(self, parent_task_id: str, owner: str) -> bool:
        """cascade 성공 후 replaying→applied CAS. 자기 lease일 때만 적용(회수된 lease는 무효).
        반환: 적용 여부."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE cascade_outbox SET state='applied', updated_at=? "
                "WHERE parent_task_id=? AND lease_owner=? AND state='replaying'",
                (time.time(), parent_task_id, owner))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def outbox_reopen(self, parent_task_id: str) -> bool:
        """라이브 cascade 도중 STOP이 authoritative가 돼 successor enqueue가 PausedError로 거부되면,
        이미 'applied'로 찍힌 부모 outbox를 'pending'으로 되돌린다 → 재개 후 executor가 저장된
        result_json으로 전체 cascade를 멱등 재실행(거부된 successor 포함)한다. lease도 초기화.
        이것이 리뷰어가 지적한 'PausedError가 result 없이 억제 기록 → replay 스킵' 문제의 해법이다:
        outbox에는 이미 result가 있으므로 reopen만 하면 result-carrying replay가 보장된다."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE cascade_outbox SET state='pending', lease_owner=NULL, attempt_id=NULL, "
                "lease_expires_at=NULL, updated_at=? "
                "WHERE parent_task_id=? AND state IN ('applied','replaying')",
                (time.time(), parent_task_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ── #314 §5: external mutation receipt (merge idempotency) ────────────

    def external_op_reserve(self, op_key: str, pr_number: Optional[int] = None) -> dict:
        """외부 GitHub mutation 시작 전 **원자적 STOP admission + reservation**(§5/재리뷰).

        같은 BEGIN IMMEDIATE 트랜잭션에서 runtime_stop을 확인해, unpaused일 때만 reservation과
        admitted_epoch를 기록하고 COMMIT한다. STOP이 이 reservation보다 먼저 linearize되면(=이미
        commit되어 있으면) 같은 write-lock 도메인에서 반드시 보여 admitted=False로 차단된다 → merge/
        comment 같은 remote mutation이 시작되지 않는다. reservation이 먼저 linearize됐다면 그 atomic
        remote operation만 완료가 허용된다(reviewer 규칙 그대로).

        반환:
          - paused/판정불가 → {"admitted": False, "state": "stop_blocked"|"error"} (mutation 금지)
          - unpaused → 기존/신규 행 dict + {"admitted": True, "reserved": 신규여부}. 호출측은
            state('reserved'/'done'/'failed')로 reconciliation."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stop = self._read_stop_row(conn)   # 같은 txn: STOP이 먼저 commit됐으면 반드시 보인다
            if stop["paused"] or self._pausejson_active():
                conn.execute("ROLLBACK")
                return {"admitted": False, "state": "stop_blocked", "reserved": False,
                        "reason": f"runtime STOP epoch={stop['epoch']}"}
            cur = conn.execute(
                "INSERT OR IGNORE INTO external_op "
                "(op_key, state, pr_number, attempt, admitted_epoch, reserved_at) "
                "VALUES (?, 'reserved', ?, 0, ?, ?)",
                (op_key, pr_number, int(stop["epoch"]), time.time()))
            newly = cur.rowcount == 1
            row = conn.execute("SELECT * FROM external_op WHERE op_key=?", (op_key,)).fetchone()
            conn.execute("COMMIT")
            d = dict(row)
            d["reserved"] = newly
            d["admitted"] = True
            return d
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            # fail-closed: 예약 트랜잭션 실패 시 mutation을 admit하지 않는다.
            logger.exception(f"external_op_reserve({op_key}) 실패 → admitted=False(fail-closed)")
            return {"admitted": False, "state": "error", "reserved": False}
        finally:
            conn.close()

    def external_op_mark(self, op_key: str, state: str, *, last_error: Optional[str] = None,
                         inc_attempt: bool = False) -> None:
        """external_op 상태 전이. state='done'이면 done_at 기록. inc_attempt면 attempt+1(재시도 backoff/cap용)."""
        conn = self._connect()
        try:
            done_at = time.time() if state == "done" else None
            if inc_attempt:
                conn.execute(
                    "UPDATE external_op SET state=?, last_error=?, attempt=attempt+1, done_at=? WHERE op_key=?",
                    (state, last_error, done_at, op_key))
            else:
                conn.execute(
                    "UPDATE external_op SET state=?, last_error=?, done_at=? WHERE op_key=?",
                    (state, last_error, done_at, op_key))
            conn.commit()
        finally:
            conn.close()

    def external_op_get(self, op_key: str) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM external_op WHERE op_key=?", (op_key,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def requeue(self, task_id: str) -> None:
        """Roll an in_progress task back to pending so it can be dequeued again."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status = 'pending' WHERE task_id = ? AND status = 'in_progress'",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def cancel(self, task_id: str) -> tuple[Optional[str], str]:
        """Cancel a task. Dependent tasks (prev_task_id points to task_id) are marked
        'orphaned' rather than cancelled — operators can manually cancel them if desired.
        Return the previous status and the pane bound at dispatch for signalling."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, dispatch_pane_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            conn.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
            # Mark pending dependents as orphaned (not cancelled) so the operator
            # can see them and decide whether to cancel or reassign.
            conn.execute(
                """
                UPDATE tasks SET status = 'orphaned'
                WHERE status IN ('pending', 'in_progress')
                AND json_extract(context, '$.prev_task_id') = ?
                """,
                (task_id,),
            )
            conn.commit()
            return ((row["status"], row["dispatch_pane_id"]) if row else (None, ""))
        finally:
            conn.close()

    def expire_stale(self, older_than_seconds: float = 600.0,
                     dry_run: bool = False) -> List[str]:
        """Cancel in_progress tasks whose last_activity_at is older than
        ``older_than_seconds``. Returns list of cancelled task_ids.

        ``dry_run=True`` returns the same candidate set **without cancelling** —
        the preview and the sweep share one predicate, so a preview can never
        list a task the sweep would spare, or spare one it would cancel.
        """
        cutoff = time.time() - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'in_progress' AND last_activity_at < ?",
                (cutoff,),
            ).fetchall()
            task_ids = [r["task_id"] for r in rows]
            if dry_run:
                return task_ids
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(
                    f"UPDATE tasks SET status = 'cancelled' WHERE task_id IN ({placeholders})",
                    task_ids,
                )
                conn.commit()
            return task_ids
        finally:
            conn.close()

    def reset_stale_to_pending(self, older_than_seconds: float = 600.0) -> List[str]:
        """Reset in_progress tasks idle > ``older_than_seconds`` back to pending.

        Unlike ``expire_stale`` (which cancels them), this returns the tasks to
        the queue so they can be picked up again. Used by ``crew recover
        --reset-stale`` (#155).
        """
        cutoff = time.time() - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'in_progress' AND last_activity_at < ?",
                (cutoff,),
            ).fetchall()
            task_ids = [r["task_id"] for r in rows]
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(
                    f"UPDATE tasks SET status = 'pending', last_activity_at = ? "
                    f"WHERE task_id IN ({placeholders})",
                    [time.time()] + task_ids,
                )
                conn.commit()
            return task_ids
        finally:
            conn.close()

    def list_orphaned(self) -> List[TaskRequest]:
        """Return all tasks with status='orphaned'."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'orphaned' ORDER BY priority ASC, created_at ASC"
            ).fetchall()
            return [
                TaskRequest(
                    task_id=r["task_id"],
                    task_type=r["task_type"],
                    description=r["description"],
                    branch=r["branch"],
                    priority=r["priority"],
                    context=json.loads(r["context"]),
                    project=r["project"] if r["project"] else "",
                )
                for r in rows
            ]
        finally:
            conn.close()

    def has_in_progress(self, task_type: str) -> bool:
        """Return True if any task of the given type is in_progress.
        Used by push-model server to decide if a role is busy."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE status = 'in_progress' AND task_type = ? LIMIT 1",
                (task_type,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def has_discuss_in_progress_for_agent(self, agent: str) -> bool:
        """Per-agent busy check for discuss tasks. Needed because discuss tasks
        fan out to different panes (one per agent) and the coarse `has_in_progress`
        would falsely mark a pane busy when a sibling panelist is mid-reply."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT context FROM tasks WHERE status = 'in_progress' AND task_type = 'discuss'"
            ).fetchall()
            for r in rows:
                try:
                    ctx = json.loads(r["context"]) if r["context"] else {}
                except Exception:
                    continue
                if ctx.get("agent") == agent:
                    return True
            return False
        finally:
            conn.close()

    def dequeue_discuss_for_agent(self, agent: str) -> Optional[TaskRequest]:
        """Atomic pending→in_progress for the oldest pending discuss task whose
        context.agent matches `agent`. Context is stored as JSON, so filtering
        happens in Python under BEGIN IMMEDIATE to keep the read+update atomic."""
        # #311/#314 STOP 전파: STOP 활성이면 discuss task도 시작하지 않는다.
        if self._stop_active_precheck():
            return None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # #314 §2: discuss claim도 dequeue와 동일하게 임계구역 내부에서 runtime_stop 재확인(TOCTOU 제거).
            if self._stop_active_in_txn(conn):
                conn.execute("ROLLBACK")
                return None
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending' AND task_type = 'discuss'
                ORDER BY priority ASC, created_at ASC
                """
            ).fetchall()
            chosen = None
            for row in rows:
                try:
                    ctx = json.loads(row["context"]) if row["context"] else {}
                except Exception:
                    continue
                if ctx.get("agent") == agent:
                    chosen = row
                    break
            if chosen is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'in_progress', last_activity_at = ? WHERE task_id = ?",
                (time.time(), chosen["task_id"]),
            )
            conn.execute("COMMIT")
            return TaskRequest(
                task_id=chosen["task_id"],
                task_type=chosen["task_type"],
                description=chosen["description"],
                branch=chosen["branch"],
                priority=chosen["priority"],
                context=json.loads(chosen["context"]),
                project=chosen["project"] if chosen["project"] else "",
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_task_context(self, task_id: str) -> dict:
        """Return the stored context dict for a task, or {} if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or not row["context"]:
                return {}
            try:
                return json.loads(row["context"])
            except Exception:
                return {}
        finally:
            conn.close()

    def list_all_with_status(self) -> List[dict]:
        """Return all tasks as raw dicts including the status and project fields."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, description, branch, priority, context, status, project, error_info "
                "FROM tasks ORDER BY priority ASC, created_at ASC"
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "description": r["description"],
                    "branch": r["branch"],
                    "priority": r["priority"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "status": r["status"],
                    "project": r["project"] if r["project"] else "",
                    "error_info": json.loads(r["error_info"]) if r["error_info"] else None,
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_tasks(self, status: str = "") -> List[TaskRequest]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC"
                ).fetchall()
            return [
                TaskRequest(
                    task_id=r["task_id"],
                    task_type=r["task_type"],
                    description=r["description"],
                    branch=r["branch"],
                    priority=r["priority"],
                    context=json.loads(r["context"]),
                    project=r["project"] if r["project"] else "",
                    status=r["status"],
                    # #213: these columns are already in `r` (SELECT *) —
                    # only actually meaningful once the task has a result,
                    # but harmless/empty-default otherwise.
                    summary=r["summary"] or "",
                    verdict=r["verdict"],
                    findings=json.loads(r["findings"]) if r["findings"] else [],
                    pr_number=r["pr_number"],
                    error_info=json.loads(r["error_info"]) if r["error_info"] else None,
                    status_changed_at=(r["status_changed_at"]
                                       if "status_changed_at" in r.keys() else 0.0),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def create_gate(self, gate: GateRequest) -> str:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO gates (id, type, message, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (gate.id, gate.type, gate.message, "pending", gate.created_at),
            )
            conn.commit()
        finally:
            conn.close()
        return gate.id

    def resolve_gate(self, gate_id: str, approved: bool) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM gates WHERE id = ?", (gate_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Gate not found: {gate_id!r}")
            if row["status"] in ("approved", "rejected"):
                conn.execute("ROLLBACK")
                raise ValueError(f"Gate {gate_id!r} is already resolved (status={row['status']!r})")
            new_status = "approved" if approved else "rejected"
            conn.execute("UPDATE gates SET status = ? WHERE id = ?", (new_status, gate_id))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_gates(self, status: str = "") -> List[GateRequest]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM gates WHERE status = ? ORDER BY created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM gates ORDER BY created_at ASC").fetchall()
            return [
                GateRequest(
                    id=r["id"],
                    type=r["type"],
                    message=r["message"],
                    status=r["status"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_result(self, task_id: str) -> Optional[TaskResult]:
        """Return TaskResult if the task is done, else None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT task_id, status, summary, verdict, findings, pr_number, context "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] not in ("completed", "failed", "needs_human"):
                return None
            context = json.loads(row["context"] or "{}")
            return TaskResult(
                task_id=row["task_id"],
                status=row["status"],
                summary=row["summary"] or "",
                verdict=row["verdict"],
                findings=json.loads(row["findings"]) if row["findings"] else [],
                pr_number=row["pr_number"],
                branch=context.get(RESULT_BRANCH_CONTEXT_KEY) or "",
                commit=context.get(RESULT_COMMIT_CONTEXT_KEY) or "",
            )
        finally:
            conn.close()

    def save_checkpoint(self, task_id: str, checkpoint_num: int, state_snapshot: dict) -> str:
        """Save a checkpoint for a task. Returns checkpoint_id."""
        checkpoint_id = f"ckpt-{task_id}-{checkpoint_num}"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (checkpoint_id, task_id, checkpoint_num, timestamp, state_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    task_id,
                    checkpoint_num,
                    time.time(),
                    json.dumps(state_snapshot),
                    time.time(),
                ),
            )
            conn.commit()
            return checkpoint_id
        finally:
            conn.close()

    def get_checkpoint(self, task_id: str, checkpoint_num: int) -> Optional[dict]:
        """Retrieve a specific checkpoint for a task."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state_snapshot FROM checkpoints WHERE task_id = ? AND checkpoint_num = ?",
                (task_id, checkpoint_num),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["state_snapshot"])
        finally:
            conn.close()

    def get_latest_checkpoint(self, task_id: str) -> Optional[tuple]:
        """Retrieve the latest checkpoint for a task. Returns (checkpoint_num, state_snapshot)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT checkpoint_num, state_snapshot FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_num DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return (row["checkpoint_num"], json.loads(row["state_snapshot"]))
        finally:
            conn.close()

    def get_task_status(self, task_id: str) -> Optional[str]:
        """Return the current DB status of a task, or None if not found (#159)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def pane_has_other_active_task(self, pane_id: str, task_id: str) -> bool:
        """Refuse an interrupt when a pane is also bound to another active task."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM tasks WHERE dispatch_pane_id = ? AND task_id != ? "
                "AND status = 'in_progress' LIMIT 1", (pane_id, task_id),
            ).fetchone() is not None
        finally:
            conn.close()

    def bump_activity(self, task_id: str, ts: Optional[float] = None) -> None:
        """Refresh last_activity_at for a task. Called by the watchdog whenever
        the agent's pane is observed busy, so the timeout/reminder clocks
        restart from the most recent sign of life."""
        if ts is None:
            ts = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET last_activity_at = ? WHERE task_id = ? AND status = 'in_progress'",
                (ts, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_push_at(self, task_id: str, ts: Optional[float] = None,
                    pane_id: str = "") -> None:
        """Record when push_fn was called for a task (bug #152).
        The watchdog uses push_at as the start of the idle clock so dispatch-queue
        wait time is excluded from the idle measurement. The pane is stored in a
        server-owned column so cancellation never trusts task-supplied context."""
        if ts is None:
            ts = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET push_at = ?, dispatch_pane_id = ? "
                "WHERE task_id = ? AND status = 'in_progress'",
                (ts, pane_id, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _reset_push_at(self, task_id: str) -> None:
        """Force push_at back to 0 (test helper — simulates MCP-dequeued tasks)."""
        conn = self._connect()
        try:
            conn.execute("UPDATE tasks SET push_at = 0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

    def list_in_progress_with_activity(self) -> List[dict]:
        """Return dicts with the fields the watchdog needs to make timeout
        decisions: task_id, task_type, context, last_activity_at, push_at, project."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, context, last_activity_at, push_at, project "
                "FROM tasks WHERE status = 'in_progress'"
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "last_activity_at": r["last_activity_at"] or 0.0,
                    "push_at": r["push_at"] or 0.0,
                    "project": r["project"] if r["project"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    def record_attribution(
        self,
        task_id: str,
        project: str = "",
        agent: str = "",
        role: str = "",
        task_type: str = "",
        worktree_path: str = "",
        repo_url: str = "",
        git_branch: str = "",
        status: str = "pending",
        # #202: durable context identity + lineage. All optional/backward
        # compatible — existing callers that only pass the fields above
        # keep working, and new rows without them default to empty/0.
        model: str = "",
        context_id: str = "",
        provider_session_id: str = "",
        context_policy: str = "",
        context_generation: int = 0,
        session_task_index: int = 0,
        previous_task_id: str = "",
        retry_of: str = "",
        fallback_of: str = "",
        started_at: float = 0.0,
    ) -> None:
        """Upsert a durable attribution record so quota systems can map token
        usage back to the project even after worktrees are torn down.

        #204: ``started_at`` is deliberately NOT in the ON CONFLICT UPDATE
        clause. A transient-error retry (#199/#205) re-dispatches the *same*
        task_id — sometimes several times — which calls this method again
        for that task_id. If started_at were overwritten on every call, each
        retry would silently erase the original first-attempt start time,
        making it impossible to distinguish real queue/retry wait time from
        execution time. The first INSERT sets it (to the caller's value, or
        `now` if the caller didn't have one — dispatch time, not queue-
        creation time); every subsequent UPSERT for that task_id leaves the
        column untouched.
        """
        codex_logs_path = (
            os.path.join(worktree_path, ".codex_local", "logs_2.sqlite")
            if agent == "codex" and worktree_path
            else ""
        )
        now = time.time()
        conn = self._connect()
        try:
            # Snapshot the coordinator authority beside the worker receipt.  The
            # worker fields above remain worker-only; a successor can therefore
            # distinguish "who dispatched" from "who executed" without prose.
            coordinator = self._coordinator_state_on(conn)
            try:
                task_row = conn.execute(
                    "SELECT context FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                task_context = json.loads(task_row["context"] or "{}") if task_row else {}
                declaration = _normalize_risk_declaration(
                    task_context.get("risk_declaration") if isinstance(task_context, dict) else None
                )
            except Exception:
                # Attribution remains a dispatch receipt if the optional
                # declaration payload is malformed or cannot be read.
                logger.exception("risk declaration attribution read failed for %s", task_id)
                declaration = _unknown_risk_declaration()
            conn.execute(
                """
                INSERT INTO task_attribution
                    (task_id, project, agent, role, task_type, worktree_path,
                     codex_logs_path, repo_url, git_branch, created_at, updated_at, status,
                     schema_version, model, context_id, provider_session_id, context_policy,
                     context_generation, session_task_index, previous_task_id, retry_of,
                     fallback_of, started_at, coordinator_id, coordinator_generation,
                     coordinator_provider, coordinator_model, coordinator_provider_session_id,
                     coordinator_checkpoint_ref, safety_or_live_change,
                     broad_architecture_change, bounded_routine_fix, human_gate_required,
                     risk_declaration_source, risk_declaration_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    model=excluded.model, context_id=excluded.context_id,
                    provider_session_id=excluded.provider_session_id,
                    context_policy=excluded.context_policy,
                    context_generation=excluded.context_generation,
                    session_task_index=excluded.session_task_index,
                    previous_task_id=excluded.previous_task_id,
                    retry_of=excluded.retry_of, fallback_of=excluded.fallback_of,
                    coordinator_id=excluded.coordinator_id,
                    coordinator_generation=excluded.coordinator_generation,
                    coordinator_provider=excluded.coordinator_provider,
                    coordinator_model=excluded.coordinator_model,
                    coordinator_provider_session_id=excluded.coordinator_provider_session_id,
                    coordinator_checkpoint_ref=excluded.coordinator_checkpoint_ref,
                    safety_or_live_change=excluded.safety_or_live_change,
                    broad_architecture_change=excluded.broad_architecture_change,
                    bounded_routine_fix=excluded.bounded_routine_fix,
                    human_gate_required=excluded.human_gate_required,
                    risk_declaration_source=excluded.risk_declaration_source,
                    risk_declaration_confidence=excluded.risk_declaration_confidence
                """,
                (task_id, project, agent, role, task_type, worktree_path,
                 codex_logs_path, repo_url, git_branch, now, now, status,
                 CONTEXT_SCHEMA_VERSION, model, context_id, provider_session_id,
                 context_policy, context_generation, session_task_index,
                 previous_task_id, retry_of, fallback_of, started_at or now,
                 coordinator["coordinator_id"], coordinator["coordinator_generation"],
                 coordinator["provider"], coordinator["model"], coordinator["provider_session_id"],
                 coordinator["checkpoint_ref"],
                 *(None if declaration[field] is None else int(declaration[field])
                   for field in RISK_DECLARATION_FIELDS),
                 declaration["declaration_source"], declaration["confidence"]),
            )
            conn.commit()
        finally:
            conn.close()

    def record_test_economics(
        self,
        task_id: str,
        *,
        effective_test_scope: str = "",
        test_scope_source: str = "",
        test_scope_hash: str = "",
        lock_wait_seconds: float = 0.0,
        lock_defer_count: int = 0,
    ) -> None:
        """Attach the tester treatment and scheduler delay to an attribution row (#278).

        After PR #275 two tasks with the same task_type, role, provider, project
        and context can cost radically different amounts depending on the
        resolved scope. Without this, quota-core cannot tell a real saving from
        a change in the diff mix, and the only alternative — parsing the
        tester's free-text summary — would make a measurement contract depend
        on agent prose.

        ⛔Written only for dispatches that actually resolved a scope. A row this
          was never called for keeps NULL in all five columns, which is the
          honest reading: unknown treatment, not "targeted".
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE task_attribution
                SET effective_test_scope=?, test_scope_source=?, test_scope_hash=?,
                    lock_wait_seconds=?, lock_defer_count=?, updated_at=?
                WHERE task_id=?
                """,
                (effective_test_scope, test_scope_source, test_scope_hash,
                 float(lock_wait_seconds), int(lock_defer_count), time.time(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def note_test_lock_defer(self, task_id: str) -> tuple:
        """Count one lock deferral for ``task_id``; return ``(count, first_at)``.

        The lock is non-blocking, so a contended test task is requeued and
        retried on the next tick — the wait is spread across N separate
        dispatch attempts rather than spent inside one. Reconstructing it later
        therefore needs the FIRST deferral's timestamp, which has to outlive the
        attempt that observed it: it lives in the task's own context so it
        survives a dispatcher restart and joins by task_id like everything else.

        ⛔Read-modify-write under BEGIN IMMEDIATE. Two dispatchers can contend
          for the same worktree — that is the situation being measured — and a
          check-then-act increment would lose one of their counts.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                conn.rollback()
                return (0, 0.0)
            try:
                context = json.loads(row["context"] or "{}")
            except Exception:
                context = {}
            if not isinstance(context, dict):
                context = {}
            count = context.get("test_lock_defer_count")
            count = (count if isinstance(count, int) else 0) + 1
            first = context.get("test_lock_first_deferred_at")
            if not isinstance(first, (int, float)) or first <= 0:
                first = time.time()
            context["test_lock_defer_count"] = count
            context["test_lock_first_deferred_at"] = first
            conn.execute("UPDATE tasks SET context = ? WHERE task_id = ?",
                         (json.dumps(context), task_id))
            conn.commit()
            return (count, float(first))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_attribution_status(self, task_id: str, status: str) -> None:
        """Update the status field of an existing attribution record."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE task_attribution SET status=?, updated_at=? WHERE task_id=?",
                (status, time.time(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_attribution_outcome(self, task_id: str, outcome: str) -> None:
        """Record the final outcome + completion timestamp for a task's
        attribution row (#202). Called once a task reaches a terminal state,
        regardless of whether that happened via the agent's own result POST
        or an internal dispatcher failure path — both funnel through
        ``submit_result`` below."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE task_attribution SET outcome=?, completed_at=?, updated_at=? WHERE task_id=?",
                (outcome, now, now, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_or_create_context(
        self,
        project: str,
        agent: str,
        worktree_path: str,
        role: str = "",
        task_id: str = "",
        force_reset: bool = False,
    ) -> dict:
        """Resolve the durable context identity for ``(project, agent,
        worktree_path)`` (#202).

        A context is scoped by agent+worktree, NOT role — see module-level
        design note in ``context_identity.py``. Mints a new ``context_id``
        (and bumps ``context_generation``) when no row exists yet for this
        key, or when ``force_reset=True`` (caller made an explicit
        ``context_reset`` request). Otherwise reuses the existing
        ``context_id`` and increments ``session_task_index``.

        Returns a dict: ``context_key, context_id, context_generation,
        session_task_index, context_policy`` (``"fresh"`` or ``"resume"``),
        ``previous_task_id``, ``provider_session_id``.
        """
        context_key = f"{project}::{agent}::{worktree_path}"
        now = time.time()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM context_state WHERE context_key = ?", (context_key,)
            ).fetchone()
            if row is None or force_reset:
                context_id = str(uuid.uuid4())
                generation = (row["context_generation"] + 1) if row else 1
                session_task_index = 1
                policy = "fresh"
                previous_task_id = row["last_task_id"] if row else None
                provider_session_id = None
                conn.execute(
                    """
                    INSERT INTO context_state
                        (context_key, project, role, agent, worktree_path, context_id,
                         context_generation, session_task_index, provider_session_id,
                         last_task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(context_key) DO UPDATE SET
                        role=excluded.role, context_id=excluded.context_id,
                        context_generation=excluded.context_generation,
                        session_task_index=excluded.session_task_index,
                        provider_session_id=excluded.provider_session_id,
                        last_task_id=excluded.last_task_id, updated_at=excluded.updated_at
                    """,
                    (context_key, project, role, agent, worktree_path, context_id,
                     generation, session_task_index, provider_session_id,
                     task_id, now, now),
                )
            else:
                context_id = row["context_id"]
                generation = row["context_generation"]
                session_task_index = row["session_task_index"] + 1
                policy = "resume"
                previous_task_id = row["last_task_id"]
                provider_session_id = row["provider_session_id"]
                conn.execute(
                    """
                    UPDATE context_state
                    SET role=?, session_task_index=?, last_task_id=?, updated_at=?
                    WHERE context_key=?
                    """,
                    (role, session_task_index, task_id, now, context_key),
                )
            conn.commit()
            return {
                "context_key": context_key,
                "context_id": context_id,
                "context_generation": generation,
                "session_task_index": session_task_index,
                "context_policy": policy,
                "previous_task_id": previous_task_id,
                "provider_session_id": provider_session_id,
            }
        finally:
            conn.close()

    def peek_context_identity(self, project: str, agent: str,
                              worktree_path: str) -> dict:
        """The recorded context identity for this triple, or ``{}`` (#297).

        A read with no side effects, like its `provider_session_id` sibling
        below and for the same reason: `get_or_create_context` mints an id,
        bumps the generation and increments the task index, so it cannot be
        used to ASK a question. An auto-clear needs to name the context it is
        clearing before anything has decided what the next one will be.

        ``{}`` when no context has been recorded yet — never a fabricated id.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT context_id, context_generation, provider_session_id "
                "FROM context_state WHERE project=? AND agent=? AND worktree_path=?",
                (project, agent, worktree_path)).fetchone()
            if row is None:
                return {}
            return {
                "context_id": row["context_id"] or "",
                "context_generation": row["context_generation"] or 0,
                "provider_session_id": row["provider_session_id"] or "",
            }
        finally:
            conn.close()

    def peek_context_provider_session_id(self, project: str, agent: str,
                                         worktree_path: str) -> str:
        """The provider session recorded for this context, WITHOUT minting one.

        `get_or_create_context` has side effects — it mints an id, bumps the
        generation, increments the task index — so it cannot be used to ask a
        question before the answer is needed. #260's cap decision has to know
        which provider session a resume would use BEFORE it decides whether to
        force a reset, and this is that read.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT provider_session_id FROM context_state "
                "WHERE project=? AND agent=? AND worktree_path=?",
                (project, agent, worktree_path)).fetchone()
            return (row["provider_session_id"] or "") if row else ""
        except Exception:  # noqa: BLE001 — a peek never breaks a dispatch
            return ""
        finally:
            conn.close()

    def update_context_provider_session_id(self, context_key: str, provider_session_id: str) -> None:
        """Record a provider-native session id observed for this context
        (#202) — e.g. parsed from claude's stream-json output. Best-effort;
        left null when the provider doesn't expose one reliably."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE context_state SET provider_session_id=?, updated_at=? WHERE context_key=?",
                (provider_session_id, time.time(), context_key),
            )
            conn.commit()
        finally:
            conn.close()

    def get_attribution(self, task_id: str) -> Optional[dict]:
        """Return the durable attribution row for ``task_id`` (#202), or
        None if no attribution was ever recorded for it. Used to correlate
        an internal dispatcher failure back to its project/role/agent/
        context_id when emitting a ``task_failed`` lifecycle event."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_attribution WHERE task_id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_tokenomics_shadow_receipt(self, task_id: str) -> Optional[dict]:
        """Return the main-branch shadow decision receipt for one task, if any."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tokenomics_shadow_receipts WHERE task_id=?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def token_cost_summary(self) -> dict:
        """Observed task_attribution token cost, grouped by issue.

        NULL means unreported: it is counted separately and never converted to
        zero. ``reasoning_tokens`` is intentionally excluded from total because
        providers report it as a component of output on some transports.
        """
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT a.task_id, a.uncached_input_tokens, a.cache_write_tokens,
                       a.cache_read_tokens, a.output_tokens, t.context
                FROM tasks t LEFT JOIN task_attribution a ON t.task_id=a.task_id
            """).fetchall()
        finally:
            conn.close()
        total = 0
        observed = 0
        by_issue: dict = {}
        for row in rows:
            parts = (row["uncached_input_tokens"], row["cache_write_tokens"],
                     row["cache_read_tokens"], row["output_tokens"])
            # A partial provider response is still an observed lower-bound; a
            # wholly NULL row remains explicitly unobserved.
            known = [int(v) for v in parts if v is not None]
            if known:
                observed += 1
                task_total = sum(known)
                total += task_total
            else:
                task_total = None
            try:
                context = json.loads(row["context"] or "{}")
            except (TypeError, ValueError):
                context = {}
            issue = context.get("issue") if isinstance(context, dict) else None
            if issue is not None:
                bucket = by_issue.setdefault(str(issue), {"observed_tasks": 0,
                                                          "unobserved_tasks": 0,
                                                          "total_tokens": 0})
                if task_total is None:
                    bucket["unobserved_tasks"] += 1
                else:
                    bucket["observed_tasks"] += 1
                    bucket["total_tokens"] += task_total
        return {"observed_tasks": observed, "unobserved_tasks": len(rows) - observed,
                "total_tokens": total, "by_issue": by_issue}

    def force_fail(self, task_id: str, summary: str, error_info: Optional[dict] = None) -> Optional[str]:
        """Mark an in_progress task as failed (used by the watchdog when a pane
        has been silent past the timeout). Returns the task_type so callers can
        push the next task to the now-idle role, or None if the row wasn't
        in_progress (e.g. result arrived just before the watchdog tick)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "in_progress":
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'failed', summary = ?, error_info = ? WHERE task_id = ?",
                (summary, json.dumps(error_info) if error_info is not None else None, task_id),
            )
            conn.execute("COMMIT")
            return row["task_type"]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def force_fail_pending(self, task_id: str, summary: str, error_info: Optional[dict] = None) -> Optional[str]:
        """Mark a pending task as failed (#145 — MCP no-client auto-fail).

        Like force_fail but operates on pending tasks rather than in_progress ones.
        Returns the task_type, or None if the row isn't pending.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'failed', summary = ?, error_info = ? WHERE task_id = ?",
                (summary, json.dumps(error_info) if error_info is not None else None, task_id),
            )
            conn.execute("COMMIT")
            return row["task_type"]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_stale_pending(self, older_than_seconds: float, now: float) -> List[dict]:
        """Return pending tasks whose created_at is more than older_than_seconds ago.

        Used by the watchdog (#136) to re-dispatch tasks that were enqueued but
        never picked up (e.g. pane was busy or push was missed).
        """
        cutoff = now - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, context, created_at FROM tasks "
                "WHERE status = 'pending' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_checkpoints(self, task_id: str) -> List[dict]:
        """List all checkpoints for a task."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT checkpoint_num, timestamp FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_num ASC",
                (task_id,),
            ).fetchall()
            return [
                {
                    "checkpoint_num": r["checkpoint_num"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
        finally:
            conn.close()
