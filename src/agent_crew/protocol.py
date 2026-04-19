import time
from dataclasses import dataclass, field
from typing import Literal, Optional

_VALID_TASK_TYPES = {"implement", "review", "test", "discuss"}
_VALID_STATUSES = {"completed", "failed", "needs_human"}
_VALID_GATE_TYPES = {"approval", "merge", "escalation"}


@dataclass
class TaskRequest:
    task_id: str
    task_type: Literal["implement", "review", "test", "discuss"]
    description: str
    branch: str = ""
    priority: int = 3
    context: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.task_type not in _VALID_TASK_TYPES:
            raise ValueError(f"Invalid task_type: {self.task_type!r}. Must be one of {_VALID_TASK_TYPES}")
        if not (1 <= self.priority <= 5):
            raise ValueError(f"Invalid priority: {self.priority!r}. Must be between 1 and 5")


@dataclass
class TaskResult:
    task_id: str
    status: Literal["completed", "failed", "needs_human"]
    summary: str
    verdict: Optional[Literal["approve", "request_changes"]] = None
    findings: list[str] = field(default_factory=list)
    pr_number: Optional[int] = None

    def __post_init__(self):
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}. Must be one of {_VALID_STATUSES}")


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
