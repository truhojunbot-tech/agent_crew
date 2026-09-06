import time
from dataclasses import dataclass, field
from typing import Literal, Optional, Union

_VALID_TASK_TYPES = {"implement", "review", "test", "discuss"}
# Result statuses: final outcomes submitted by agents
_VALID_RESULT_STATUSES = {"completed", "failed", "needs_human", "timed_out", "blocked"}
# For backward compatibility, keep old name
_VALID_STATUSES = _VALID_RESULT_STATUSES
_VALID_GATE_TYPES = {"approval", "merge", "escalation"}


def normalize_pr_number(value) -> Optional[int]:
    """``value`` as a PR number, or ``None`` when it does not name one.

    PR numbers reach us untyped from two directions — out of a task's JSON
    context column, and off an agent-authored result — and agents write them
    the way humans do. `268`, `"268"`, `"#268"` and `" 268 "` all name the same
    PR and must compare equal, or the #268 cross-check fires on spelling
    instead of on substance.

    ⛔`bool` is short-circuited before `int()`: in Python `True` is `1`, and a
      `pr_number=True` quietly becoming "PR #1" would invent a disagreement
      with whatever PR the task was really about.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().lstrip("#").strip()
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass
class TaskRequest:
    task_id: str
    task_type: Literal["implement", "review", "test", "discuss"]
    description: str
    branch: str = ""
    priority: int = 3
    context: dict = field(default_factory=dict)
    project: str = ""  # owner/name form, e.g. "org/myrepo". Used to detect cross-project routing.
    status: str = "pending"  # DB status; populated by list_tasks, not stored on enqueue
    # Result-preview fields (#213). Same pattern as `status` above: populated
    # by list_tasks/get_task from the DB row when the task has finished, never
    # sent or read on enqueue. Without these, GET /tasks and GET /tasks/{id}
    # returned the request shape only — summary/verdict/findings/error_info
    # sat filled in the DB but never reached a caller polling the HTTP API,
    # which read as "the result was lost" and caused the same work to be
    # re-run.
    summary: str = ""
    verdict: Optional[Literal["approve", "request_changes"]] = None
    findings: list[str] = field(default_factory=list)
    pr_number: Optional[int] = None
    error_info: Optional[dict] = None
    #: When the status last changed, epoch seconds (#265). A consumer that read
    #: `failed` at notification time and sees this move afterwards knows the
    #: verdict was revised — the transition used to be completely silent.
    status_changed_at: float = 0.0

    def __post_init__(self):
        if self.task_type not in _VALID_TASK_TYPES:
            raise ValueError(f"Invalid task_type: {self.task_type!r}. Must be one of {_VALID_TASK_TYPES}")
        if not (1 <= self.priority <= 5):
            raise ValueError(f"Invalid priority: {self.priority!r}. Must be between 1 and 5")


@dataclass
class TaskResult:
    task_id: str
    status: Literal["completed", "failed", "needs_human", "timed_out", "blocked"]
    summary: str
    verdict: Optional[Literal["approve", "request_changes"]] = None
    findings: list[str] = field(default_factory=list)
    #: ⛔Accepts a `str` on the way IN and is always `int | None` on the way OUT
    #: — see `__post_init__`. Typed `Optional[int]`, FastAPI rejected `"#268"`
    #: with a 422 before any normalisation could run (review of PR #270), so
    #: the one spelling the guard was written to tolerate was the one the
    #: endpoint threw the entire result away over.
    #: ⛔`bool` is in the union so that pydantic CANNOT quietly remove it. With
    #: `Union[int, str]` alone, lax validation turned JSON `true` into `1`
    #: before `__post_init__` ran, so the "True is not PR #1" guard was intact
    #: in the type and defeated at the endpoint (review of PR #270). Listing
    #: `bool` does advertise it in the schema — the trade is deliberate: we
    #: accept it syntactically so we can refuse it explicitly, instead of
    #: coercing it into a plausible PR number nobody named.
    pr_number: Optional[Union[bool, int, str]] = None
    retry_count: int = 0  # Track number of retry attempts
    error_info: Optional[dict] = None  # Structured error payload for debugging (#167)

    def __post_init__(self):
        if self.status not in _VALID_RESULT_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}. Must be one of {_VALID_RESULT_STATUSES}")
        if self.retry_count < 0:
            raise ValueError(f"Invalid retry_count: {self.retry_count!r}. Must be >= 0")
        # Normalise the SPELLING, not the value. A string that names a PR
        # becomes the int every consumer already expects (the queue writes an
        # INTEGER column; the cascade calls `int()` on it), an empty string
        # means "no PR", and anything else is a malformed request the caller
        # has to hear about — silently storing `None` there would drop exactly
        # the signal #268 exists to preserve. Non-strings are left untouched,
        # so nothing that already worked behaves differently.
        if isinstance(self.pr_number, bool):
            # A type error, not a spelling — same class as "not-a-pr", and
            # refused the same way. `1` is the dangerous outcome precisely
            # because it is PLAUSIBLE: `"not-a-pr"` is obviously wrong to
            # anyone reading the row, whereas `pr_number: 1` reads as a
            # considered claim about PR #1 and the cascade calls `int()` on it.
            raise ValueError(
                f"Invalid pr_number: {self.pr_number!r}. A boolean does not name "
                f"a PR — omit the field if there is no PR.")
        if isinstance(self.pr_number, str):
            raw = self.pr_number.strip()
            if not raw:
                self.pr_number = None
            else:
                normalized = normalize_pr_number(raw)
                if normalized is None:
                    raise ValueError(
                        f"Invalid pr_number: {self.pr_number!r}. Must name a PR "
                        f'(268, "268" or "#268") or be omitted.')
                self.pr_number = normalized


@dataclass
class GateRequest:
    id: str
    type: Literal["approval", "merge", "escalation"]
    message: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.type not in _VALID_GATE_TYPES:
            raise ValueError(f"Invalid type: {self.type!r}. Must be one of {_VALID_GATE_TYPES}")
