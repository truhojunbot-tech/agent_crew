"""Validation for persisted crew listener ports (#362)."""

from typing import Any


MIN_ALLOCATOR_PORT = 1024
MAX_TCP_PORT = 65535


def require_project_port(port: Any, project: str) -> int:
    """Return a persisted crew port, or reject it before it reaches disk."""
    if isinstance(port, bool) or not isinstance(port, int) or not (
        MIN_ALLOCATOR_PORT <= port <= MAX_TCP_PORT
    ):
        raise ValueError(
            f"project {project!r}: invalid crew port {port!r}; expected "
            f"{MIN_ALLOCATOR_PORT}-{MAX_TCP_PORT}, never 0"
        )
    return port
